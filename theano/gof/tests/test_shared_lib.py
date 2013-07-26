import os

import numpy as np

import theano


def test_basic():
    a = theano.tensor.vector()
    b = theano.tensor.vector()
    for inps, out in [([a], theano.tensor.exp(a)),  # 1 input/1 outputs
                      ([a, b], a + b),  # 2 inputs
                      ((a, b), [a, b]),  # 2 outputs, 2 deepcopy ops
                      ((a, b), [a + b, a - b]),
                  ]:
        f = theano.function(inps, out, theano.Mode(linker='c'),
                            on_unused_input='ignore')
        theano.printing.debugprint(f, print_type=True)
        #filename = f.fn.thunks[0].filename  # with linker=vm
        filename = f.fn.filename  # with linker=c
        print filename

        #theano.shared_lib(f, name='libtheano_exp')
        #f(np.arange(10))
        x = os.system(os.path.join(os.path.split(filename)[0], 'exec'))
        assert x == 0, "The executable crashed!"

        # Test raise error if no c code
        try:
            theano.function((a,), [theano.tensor.argmax(a)],
                            theano.Mode(linker='c'),
                            on_unused_input='ignore')
            assert False, "Expected an error"
        except NotImplementedError:
            pass