import os.path
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)) + os.path.sep + '..')

from cudaconv import cuda_conv_generic
import torch
import numpy
from torch.autograd import Variable

# Computation are made in float32
dtype = torch.FloatTensor 

# See github.com/pytorch/pytorch/pull/1016 , pytorch.org/docs/0.2.0/notes/extending.html
# for reference on the forward-backward syntax
class GenericKernelProduct(torch.autograd.Function):
	""" 
	Computes a Generic Kernel Product specified by a formula (string) such as :
	formula = "Scal< Square<Scalprod<U,V>>, Scal< Exp< Scal<Constant<C>, Minus<SqNorm2<Subtract<X,Y>>> > >,  B> >"
	"""
	
	@staticmethod
	def forward(ctx, aliases, formula, signature, sum_index, *args):
		""" 
		Computes a Generic Kernel Product specified by a formula (string) such as :
		```
		formula = "Scal< Square<Scalprod<U,V>>, Scal< Exp< Scal<Constant<C>, Minus<SqNorm2<Subtract<X,Y>>> > >,  B> >"
		```
		i.e.       <U,V>^2 * exp(-C*|X-Y|^2 ) * B 
		
		aliases is a list of strings, which specifies "who is who"; for example :
		```
		aliases = [ "DIMPOINT = 3", "DIMVECT = 4", "DIMOUT = 5",
					"C = Param<0>"          ,   # 1st parameter
					"X = Var<0,DIMPOINT,0>" ,   # 1st variable, dim 3, indexed by i
					"Y = Var<1,DIMPOINT,1>" ,   # 2nd variable, dim 3, indexed by j
					"U = Var<2,DIMVECT ,0>" ,   # 3rd variable, dim 4, indexed by i
					"V = Var<3,DIMVECT ,1>" ,   # 4th variable, dim 4, indexed by j
					"B = Var<4,DIMOUT  ,1>" ]   # 5th variable, dim 5, indexed by j
		```
		
		signature is a list of (DIM, CAT) integer pairs allowing the user to specify
		the respective dimensions of the output (head) and variables (tail of the list).
		Remember that CAT=0 for "x_i" indexing  variables,
		              CAT=1 for "y_j" summation variables,
		              CAT=2 for parameters.
		For instance,
		```
		signature = [ (5,0), (1,2), (3,0), (3,1), (4,0), (4,1), (5,1) ]
		# stands for:  R_i ,   C  ,  X_i  , Y_j  , U_i  , V_j  , B_j   .
		```
		
		Theoretically, signature could be inferred from formula+aliases...
		But asking the user to provide it explicitely is a good way to let him double-check
		his formula, and makes debugging easier.
		
		With the values defined above,
		```
		genconv = GenericKernelProduct().apply
		R = genconv( aliases, formula, signature, 0, C, X, Y, U, V, B )
		```
		is a legal call, where :
		- C is a scalar              (torch Variable)
		- X is a nx-by-3 float array (torch Variable)
		- Y is a ny-by-3 float array (torch Variable)
		- U is a nx-by-4 float array (torch Variable)
		- V is a ny-by-4 float array (torch Variable)
		- B is a ny-by-5 float array (torch Variable)
		which outputs:
		- R, an  nx-by-5 float array (torch Variable)
		
		(nx and ny are automatically inferred from the data; 
		an error is thrown if the lengths of the input arrays are not compatible with each other)
		
		Eventually, in this example, we've computed a "Gaussian-CauchyBinet varifold kernel"
		with a signal B of dimension 4 :
		
		R_i = \sum_j <U_i,V_j>^2 * exp(-C*|X_i-Y_j|^2 ) * B_j
		
		Its derivatives wrt. X, Y, U, V, B are automatically computed (symbolically, without backprop), 
		and are accessible using standard PyTorch syntax.
		
		N.B.: The data type (float v. double) is inferred automatically from the PyTorch type of args.
			  The CPU/GPU mode is chosen automatically.
			  The grid_scheme  (1D v. 2D) is chosen according to heuristic rules.
		"""
		# Save everything to compute the gradient -----------------------------------------------
		# N.B.: relying on the "ctx.saved_variables" attribute is necessary
		#       if you want to be able to differentiate the output of the backward
		#       once again. It helps pytorch to keep track of "who is who".
		ctx.save_for_backward( *args ) # Call at most once in the "forward".
		ctx.aliases   = aliases
		ctx.formula   = formula
		ctx.signature = signature
		ctx.sum_index = sum_index
		
		# Get the size nx by looping on the signature until we've found an "x_i" ----------------
		n = -1
		for ( index, (dim,cat) ) in enumerate(signature[1:]) : # Omit the output
			if cat == sum_index :
				n = len( args[index] ) # Lengths compatibility is done by cuda_conv_generic
				break
		if n == -1 and sum_index == 0: raise ValueError("The signature should contain at least one indexing argument x_i.")
		if n == -1 and sum_index == 1: raise ValueError("The signature should contain at least one indexing argument y_j.")
		
		# Data Conversion (only CPU via numpy implented at the moment) --------------------------
		
		args_conv = [ arg.numpy() for arg in args]
		
		# Actual computation --------------------------------------------------------------------
		result  = torch.zeros( n * signature[0][0] ).type(dtype) # Init the output of the convolution
		cuda_conv_generic(formula, result, *args_conv,           # Inplace CUDA routine
		                  aliases   = aliases, sum_index   = sum_index,
		                  cuda_type = "float", grid_scheme = "2D") 
		result  = result.view( n, signature[0][0] )
		return result
	
	@staticmethod
	def backward(ctx, G):
		"""
		Backward scheme.
		G has the same shape (and thus, dim-cat signature) as the formula's output.
		"""
		aliases   = ctx.aliases
		formula   = ctx.formula
		signature = ctx.signature
		sum_index = ctx.sum_index
		args      = ctx.saved_variables # Unwrap the saved variables
		
		# Compute the number of arguments which are not parameters
		nargs = 0 ; 
		for sig in signature[1:] : 
			if sig[1] != 2 : nargs += 1
		
		# If formula takes 5 arguments (numbered from 0 to 4), then the gradient
		# wrt. the output, G, should be given as a 6-th variable (numbered 5),
		# with the same dim-cat as the formula's output. 
		eta   = "Var<"+str(nargs)+","+str(signature[0][0])+","+str(signature[0][1])+">"
		grads = [] ; ind = 0
		for sig in signature[1:] :
			if sig[1] == 2 : # we're referring to a parameter
				grads.append(None)
			else :
				# adding new aliases is waaaaay too dangerous if we want to compute
				# second derivatives, etc. So we make explicit references to Var<ind,dim,cat> instead.
				var         = "Var<"+str(ind)+","+str(sig[0])+","+str(sig[1])+">"
				formula_g   = "Grad< "+ formula +","+ var +","+ eta +">"
				signature_g = [ sig ] + signature[1:] + signature[:1]
				sumindex_g  = 1 - sig[1]
				args_g      = args + [G]
				
				# N.B.: if I understand PyTorch's doc, we should redefine this function every time we use it?
				genconv = GenericKernelProduct().apply  
				grads.append( genconv( aliases, formula_g, signature_g, sumindex_g, *args_g )  )
				ind += 1 # increment the variable count
		
		# Grads wrt. aliases, formula, signature, sum_index, *args
		return      (   None,    None,      None,      None, *grads )


if __name__ == "__main__":
	# from visualize import make_dot
	
	backend = "libkp" # Other value : 'pytorch'
	
	if   backend == "libkp" :
		kernel_product        = KernelProduct().apply
		kernel_product_grad_x = KernelProductGrad_x().apply
	elif backend == "pytorch" :
		def kernel_product(s, x, y, b, kernel) :
			x_col = x.unsqueeze(1) # Theano : x.dimshuffle(0, 'x', 1)
			y_lin = y.unsqueeze(0) # Theano : y.dimshuffle('x', 0, 1)
			sq    = torch.sum( (x_col - y_lin)**2 , 2 )
			K_xy  = torch.exp( -sq / (s**2))
			return K_xy @ b
			
			
			
	#--------------------------------------------------#
	# Init variables to get a minimal working example:
	#--------------------------------------------------#
	dtype = torch.FloatTensor
	
	N = 10 ; M = 15 ; D = 3 ; E = 3
	
	e = .6 * torch.linspace(  0, 5,N*D).type(dtype).view(N,D)
	e = torch.autograd.Variable(e, requires_grad = True)
	
	a = .6 * torch.linspace(  0, 5,N*E).type(dtype).view(N,E)
	a = torch.autograd.Variable(a, requires_grad = True)
	
	x = .6 * torch.linspace(  0, 5,N*D).type(dtype).view(N,D)
	x = torch.autograd.Variable(x, requires_grad = True)
	
	y = .2 * torch.linspace(  0, 5,M*D).type(dtype).view(M,D)
	y = torch.autograd.Variable(y, requires_grad = True)
	
	b = .6 * torch.linspace(-.2,.2,M*E).type(dtype).view(M,E)
	b = torch.autograd.Variable(b, requires_grad = True)
	
	s = torch.Tensor([2.5]).type(dtype)
	s = torch.autograd.Variable(s, requires_grad = False)
	
	#--------------------------------------------------#
	# check the class KernelProductGrad_x routines
	#--------------------------------------------------#
	
	if False :
		grad_x = kernel_product_grad_x(s, a, x, y, b, "gaussian")
		(grad_xa, grad_xx, grad_xy, grad_xb) = torch.autograd.grad( grad_x, (a,x,y,b), e)
	
	if False :
		print("grad_x  :\n",  grad_x.data.numpy())
		print("grad_xa :\n", grad_xa.data.numpy())
		print("grad_xx :\n", grad_xx.data.numpy())
		print("grad_xy :\n", grad_xy.data.numpy())
		print("grad_xb :\n", grad_xb.data.numpy())
	
	
	#--------------------------------------------------#
	# check the class KernelProduct
	#--------------------------------------------------#
	def Ham(q,p) :
		Kq_p  = kernel_product(s,q,q,p, "gaussian")
		# make_dot(Kq_p, {'q':q, 'p':p, 's':s}).render('graphs/Kqp_'+backend+'.pdf', view=True)
		return torch.dot( p.view(-1), Kq_p.view(-1) )
	
	ham0   = Ham(y, b)
	# make_dot(ham0, {'y':y, 'b':b, 's':s}).render('graphs/ham0_'+backend+'.pdf', view=True)
	
	print('----------------------------------------------------')
	print("Ham0:")
	print(ham0)
	
	grad_y = torch.autograd.grad(ham0,y,create_graph = True)[0]
	grad_b = torch.autograd.grad(ham0,b,create_graph = True)[0]
	
	print('grad_y  :\n', grad_y.data.numpy())
	print('grad_b  :\n', grad_b.data.numpy())
	
	
	# make_dot(grad_y, {'y':y, 'b':b, 's':s}).render('graphs/grad_y_'+backend+'.pdf', view=True)
	print('grad_y :\n', grad_y.data.numpy())
	
	if False :
		def to_check( X, Y, B ):
			return kernel_product(s, X, Y, B, "gaussian")
		gc = torch.autograd.gradcheck(to_check, inputs=(x, y, b) , eps=1e-4, atol=1e-3, rtol=1e-3 )
		print('Gradcheck for Hamiltonian: ',gc)
		print('\n')

	#--------------------------------------------------#
	# check that we are able to compute derivatives with autograd
	#--------------------------------------------------#
	
	if False :
		grad_b_sum = torch.dot(grad_b.view(-1), grad_b.view(-1))
		# make_dot(grad_b_sum, {'y':y, 'b':b, 's':s}).render('graphs/grad_b_sum_'+backend+'.pdf', view=True)
		print('grad_b_sum :\n', grad_b_sum.data.numpy())
		
		grad_b_sum_b  = torch.autograd.grad(grad_b_sum,b,create_graph = True)[0]
		make_dot(grad_b_sum_b, {'y':y, 'b':b, 's':s}).render('graphs/grad_b_sum_b_'+backend+'.pdf', view=True)
		print('grad_b_sum_b :\n', grad_b_sum_b.data.numpy())
	
	if True :
		# N.B. : As of October 2017, there's clearly a type problem within pytorch's implementation
		#        of sum's backward operator - I looks as though they naively pass an array of
		#        "1" to the backward operator
		# grad_y_sum = grad_y.sum() # backward will be randomish, due to type conversion issues
		Ones = Variable(torch.ones( grad_y.numel() ).type(dtype))
		grad_y_sum = torch.dot(grad_y.view(-1), Ones )
		# make_dot(grad_y_sum, {'y':y, 'b':b, 's':s}).render('graphs/grad_y_sum_'+backend+'.pdf', view=True)
		print('grad_y_sum :\n', grad_y_sum.data.numpy())
		
		grad_y_sum_y  = torch.autograd.grad(grad_y_sum,y,create_graph = True)[0]
		# make_dot(grad_y_sum_y, {'y':y, 'b':b, 's':s}).render('graphs/grad_y_sum_y_'+backend+'.pdf', view=True)
		print('grad_y_sum_y :\n', grad_y_sum_y.data.numpy())
		
		
		grad_y_sum_b  = torch.autograd.grad(grad_y_sum,b,create_graph = True)[0]
		# make_dot(grad_y_sum_b, {'y':y, 'b':b, 's':s}).render('graphs/grad_y_sum_b_'+backend+'.pdf', view=True)
		print('grad_y_sum_b :\n', grad_y_sum_b.data.numpy())
	
	
	#--------------------------------------------------#
	# check that we are able to compute derivatives with autograd
	#--------------------------------------------------#
	if False :
		q1 = .6 * torch.linspace(0,5,n*d).type(dtype).view(n,d)
		q1 = torch.autograd.Variable(q1, requires_grad = True)

		p1 = .5 * torch.linspace(-.2,.2,n*d).type(dtype).view(n,d)
		p1 = torch.autograd.Variable(p1, requires_grad = True)
		sh = torch.sin(ham)

		gsh = torch.autograd.grad( sh , p1, create_graph = True)[0]
		print('derivative of sin(ham): ', gsh)
		print(gsh.volatile)

		ngsh = gsh.sum()

		ggsh = torch.autograd.grad( ngsh , p1)
		print('derivative of sin(ham): ', ggsh)