"""
Defines Linkers that deal with C implementations.

"""

from __future__ import print_function

# Python imports
from copy import copy
import os
import re
import sys
import logging

import numpy
import theano
from theano import config
from theano.compat import PY3
from theano.compat import izip
from six import string_types, reraise
from six.moves import StringIO, xrange

# Note that we need to do this before importing cutils, since when there is
# no theano cache dir initialized yet, importing cutils may require compilation
# of cutils_ext.
from theano.configparser import AddConfigVar, StrParam

# gof imports
from theano.gof import graph
from theano.gof import link
from theano.gof import utils
from theano.gof import cmodule
from theano.gof.compilelock import get_lock, release_lock
from theano.gof.callcache import CallCache

AddConfigVar('gcc.cxxflags',
             "Extra compiler flags for gcc",
             StrParam(""))


_logger = logging.getLogger("theano.gof.cc")


run_cthunk = None  # Will be imported only when needed.


def get_module_cache(init_args=None):
    """

    Parameters
    ----------
    init_args
        If not None, the (k, v) pairs in this dictionary will be forwarded to
        the ModuleCache constructor as keyword arguments.

    """
    return cmodule.get_module_cache(config.compiledir, init_args=init_args)


_persistent_module_cache = None


def get_persistent_module_cache():
    global _persistent_module_cache
    if _persistent_module_cache is None:
        _persistent_module_cache = CallCache(os.path.join(config.compiledir,
                                                          'persistent_cache'))
    return _persistent_module_cache


class CodeBlock:
    """
    WRITEME

    Represents a computation unit composed of declare, behavior, and cleanup.

    The constructor initializes a L{CodeBlock} with templatized declare,
    behavior and cleanup. The sub parameter will be used in the other
    arguments' templates. sub should contain a key called 'id' that maps to an
    identifier for this block. The identifier will be used to determine the
    failure code and a label to jump to. It should also contain a key called
    'failure_var' that contains the name of the variable that contains the error
    code.

    Parameters
    ----------
    declare
        C code that declares variables for use by the computation.
    behavior
        C code that performs the computation.
    cleanup
        C code that cleans up things allocated or incref-ed in behavior.

    """

    def __init__(self, declare, behavior, cleanup, sub):
        self.declare = declare
        self.behavior = behavior
        # the dummy is because gcc throws an error when a label's
        # right next to a closing brace (maybe there's an ignore flag
        # for that...)
        # we need the label even if cleanup is empty because the
        # behavior block jumps there on failure
        self.cleanup = ("__label_%(id)i:\n" % sub + cleanup +
                        "\ndouble __DUMMY_%(id)i;\n" % sub)  # % sub


def failure_code(sub):
    """
    Code contained in sub['fail'], usually substituted for %(fail)s.

    It sets information about current error, then goto the code
    actually handling the failure, which is defined in struct_gen().

    """
    return '''{
        %(failure_var)s = %(id)s;
        if (!PyErr_Occurred()) {
            PyErr_SetString(PyExc_RuntimeError,
                "Unexpected error in an Op's C code. "
                "No Python exception was set.");
            }
        goto __label_%(id)i;}''' % sub


def failure_code_init(sub):
    """
    Code for failure in the struct init.

    """
    return '''{
        if (!PyErr_Occurred()) {
            PyErr_SetString(PyExc_RuntimeError,
                "Unexpected error in an Op's C code. "
                "No Python exception was set.");
            }
        return %(id)d;
}''' % sub


def code_gen(blocks):
    """
    WRITEME

    From a list of L{CodeBlock} instances, returns a string
    that executes them all in sequence. eg for C{(decl1, task1,
    cleanup1)} and C{(decl2, task2, cleanup2)} the returned string
    will be of the form:

        decl1
        decl2
        {
         task1
         {
          task2
          cleanup2
         }
         cleanup1
        }

    """
    decl = ""
    head = ""
    tail = ""
    for block in blocks:
        decl += block.declare
        head = head + ("\n{\n%s" % block.behavior)
        tail = ("%s\n}\n" % block.cleanup) + tail
    return decl + head + tail


def struct_gen(args, struct_builders, blocks, sub, is_c_callable=False):
    """
    WRITEME

    Generates a struct conforming to the following specifications:

    Parameters
    ----------
     args
        All of the PyObject* type, stored in the struct
        they represent the storage and must be length 1 python lists.
     struct_builders
        List of L{CodeBlock} instances such that
        * declarations are in the struct
        * behavior is in the constructor
        * cleanup is in the destructor
     blocks
        List of CodeBlock instances such that
        * declarations, behavior and cleanup are in the run()
        method of the struct
     sub
        Dictionary used to template the struct.
        * failure_var -> must contain a variable name to use for
        the failure code.
    is_c_callable
         True if the code generate should be used ad share library

    Returns
    -------
    object
        In a nutshell, this returns code for a struct that represents
        a function with state. The state's initialization and destruction
        are handled by struct_builders and the actual behavior of the
        function is handled by blocks.

    """
    struct_decl = ""
    struct_init_head = ""
    struct_init_tail = ""
    struct_cleanup = ""

    for block in struct_builders:
        # decl are declarations that go in the struct
        # init_head are in the constructor
        # init_tail and cleanup do the same thing, but the former will
        #     be executed if any step in the constructor fails and the
        #     latter only at destruction time.
        struct_decl += block.declare
        struct_init_head = struct_init_head + ("\n%s" % block.behavior)
        struct_cleanup += block.cleanup

    behavior = code_gen(blocks)

    # declares the storage
    storage_decl = "\n".join(["PyObject* %s;" % arg for arg in args])
    # in the constructor, sets the storage to the arguments
    storage_set = "\n".join(["this->%s = %s;" % (arg, arg) for arg in args])
    # increments the storage's refcount in the constructor
    storage_incref = "\n".join(["Py_XINCREF(%s);" % arg for arg in args])
    # decrements the storage's refcount in the destructor
    storage_decref = "\n".join(["Py_XDECREF(this->%s);" % arg for arg in args])

    args_names = ", ".join(args)
    args_decl = ", ".join(["PyObject* %s" % arg for arg in args])

    # The following code stores the exception data in __ERROR, which
    # is a special field of the struct. __ERROR is a list of length 3
    # that holds the type, the value and the traceback. After storing
    # the error, we return the failure code so we know which code
    # block failed.
    do_return = """
        if (%(failure_var)s) {
            // When there is a failure, this code puts the exception
            // in __ERROR.
            PyObject* err_type = NULL;
            PyObject* err_msg = NULL;
            PyObject* err_traceback = NULL;
            PyErr_Fetch(&err_type, &err_msg, &err_traceback);
            if (!err_type) {err_type = Py_None;Py_INCREF(Py_None);}
            if (!err_msg) {err_msg = Py_None; Py_INCREF(Py_None);}
            if (!err_traceback) {err_traceback = Py_None; Py_INCREF(Py_None);}
            PyObject* old_err_type = PyList_GET_ITEM(__ERROR, 0);
            PyObject* old_err_msg = PyList_GET_ITEM(__ERROR, 1);
            PyObject* old_err_traceback = PyList_GET_ITEM(__ERROR, 2);
            PyList_SET_ITEM(__ERROR, 0, err_type);
            PyList_SET_ITEM(__ERROR, 1, err_msg);
            PyList_SET_ITEM(__ERROR, 2, err_traceback);
            {Py_XDECREF(old_err_type);}
            {Py_XDECREF(old_err_msg);}
            {Py_XDECREF(old_err_traceback);}
        }
        // The failure code is returned to index what code block failed.
        return %(failure_var)s;
        """ % sub

    sub = dict(sub)
    sub.update(locals())

    # TODO: add some error checking to make sure storage_<x> are
    # 1-element lists and __ERROR is a 3-elements list.

    struct_code = """

    struct %(name)s {
        PyObject* __ERROR;

        %(storage_decl)s
        %(struct_decl)s

        %(name)s() {}
        ~%(name)s(void) {
            cleanup();
        }

        int init(PyObject* __ERROR, %(args_decl)s) {
            %(storage_incref)s
            %(storage_set)s
            %(struct_init_head)s
            this->__ERROR = __ERROR;
            return 0;
        }// end init()
        void cleanup(void) {
            %(struct_cleanup)s
            %(storage_decref)s
        } // end cleanup()
        int run(void);
    };
    """ % sub
    if is_c_callable:
        run_code = """
            DllExport int %(name)s::run(void) {
                      int %(failure_var)s = 0;
                      %(behavior)s
                      %(do_return)s
                      }
        """ % sub
    else:
        run_code = """
            int %(name)s::run(void) {
                int %(failure_var)s = 0;
                %(behavior)s
                %(do_return)s
         }
     """ % sub

    return struct_code, run_code


# The get_<x> functions complete the return value of r.get_<x>()
# with handling of the py_<name> variable.

def get_nothing(r, name, sub):
    """
    WRITEME

    """
    return ""


def get_c_declare(r, name, sub):
    """
    Wrapper around c_declare that declares py_name.
    """
    # The declaration will be used by the Apply node that
    # is computing it (`r.owner`), and by each of the clients.
    # If some of these have `check_input=True` in their `.op`,
    # it means they need `r`'s dtype to be declared, so
    # we have to pass `check_input=True` to `c_declare`.
    if ((any([getattr(c.op, 'check_input', config.check_input)
              for (c, _) in r.clients
              if not isinstance(c, string_types)]) or
         (r.owner and
          getattr(r.owner.op, 'check_input', config.check_input)))):
        c_declare = r.type.c_declare(name, sub, True)
    else:
        c_declare = r.type.c_declare(name, sub, False)
    pre = """
    PyObject* py_%(name)s;
    """ % locals()
    return pre + c_declare


def get_c_init(r, name, sub):
    """
    Wrapper around c_init that initializes py_name to Py_None.

    """
    pre = "" """
    py_%(name)s = Py_None;
    {Py_XINCREF(py_%(name)s);}
    """ % locals()
    return pre + r.type.c_init(name, sub)


def get_c_extract(r, name, sub):
    """
    Wrapper around c_extract that initializes py_name from storage.

    """
    # `c_extract` is called when getting the value of an apply node's
    # input from the compute map, before being used by its clients.
    # If one of the clients has `check_input=True`, we need to perform
    # checks on the variable.
    # However that code is not used by C code of the apply node creating
    # this variable, so there is no need to check `r.owner.op.check_input`.
    if any([getattr(c.op, 'check_input', config.check_input)
            for (c, _) in r.clients
            if not isinstance(c, string_types)]):
        # check_broadcast is just an hack to easily remove just the
        # broadcast check on the old GPU back-end. This check isn't
        # done in the new GPU back-end or on the CPU.
        if any([getattr(c.op, 'check_broadcast', True)
                for (c, _) in r.clients
                if not isinstance(c, string_types)]):
            c_extract = r.type.c_extract(name, sub, True)
        else:
            try:
                c_extract = r.type.c_extract(
                    name, sub, True,
                    check_broadcast=False)
            except TypeError as e:
                c_extract = r.type.c_extract(name, sub, True)
    else:
        c_extract = r.type.c_extract(name, sub, False)

    pre = """
    py_%(name)s = PyList_GET_ITEM(storage_%(name)s, 0);
    {Py_XINCREF(py_%(name)s);}
    """ % locals()
    return pre + c_extract


def get_c_extract_out(r, name, sub):
    """
    Wrapper around c_extract_out that initializes py_name from storage.

    """
    # `c_extract_out` is used to extract an output variable from
    # the compute map, to be used as pre-allocated memory for `r`
    # before its value gets computed.
    # If the node producing `r` has `check_inputs=True`, it may
    # also perform type checks on the initial value of the output,
    # so we need to pass `check_input=True` to `c_extract_out`.
    # However, that code is not used by potential clients of `r`,
    # so we do not need to check them.
    check_input = getattr(r.owner.op, 'check_input', config.check_input)
    # check_broadcast is just an hack to easily remove just the
    # broadcast check on the old GPU back-end. This check isn't
    # done in the new GPU back-end or on the CPU.
    if getattr(r.owner.op, 'check_broadcast', True):
        c_extract = r.type.c_extract_out(name, sub, check_input)
    else:
        try:
            c_extract = r.type.c_extract_out(name, sub, check_input,
                                             check_broadcast=False)
        except TypeError as e:
            c_extract = r.type.c_extract_out(name, sub, check_input)

    pre = """
    py_%(name)s = PyList_GET_ITEM(storage_%(name)s, 0);
    {Py_XINCREF(py_%(name)s);}
    """ % locals()
    return pre + c_extract


def get_c_cleanup(r, name, sub):
    """
    Wrapper around c_cleanup that decrefs py_name.

    """
    post = """
    {Py_XDECREF(py_%(name)s);}
    """ % locals()
    return r.type.c_cleanup(name, sub) + post


def get_c_sync(r, name, sub):
    """
    Wrapper around c_sync that syncs py_name with storage.

    """
    return """
    if (!%(failure_var)s) {
      %(sync)s
      PyObject* old = PyList_GET_ITEM(storage_%(name)s, 0);
      {Py_XINCREF(py_%(name)s);}
      PyList_SET_ITEM(storage_%(name)s, 0, py_%(name)s);
      {Py_XDECREF(old);}
    }
    """ % dict(sync=r.type.c_sync(name, sub), name=name, **sub)


def apply_policy(policy, r, name, sub):
    """
    WRITEME

    Parameters
    ----------
    policy
        List of functions that map a L{Variable} to a string,
        or a single such function.
    r: L{Variable}

    Returns
    -------
    object
        C{policy[0](r) + policy[1](r) + ...}.

    """
    if isinstance(policy, (list, tuple)):
        ret = ""
        for sub_policy in policy:
            ret += sub_policy(r, name, sub)
        return ret
    return policy(r, name, sub)


def struct_variable_codeblocks(variable, policies, id, symbol_table, sub):
    """
    WRITEME

    Parameters
    ----------
    variable : a Variable
    policies : a pair of tuples
        (declare_policy, behavior_policy, cleanup_policy) -- at construction.
        (declare_policy, behavior_policy, cleanup_policy)) -- at execution.
        The first list will produce an element of the 'struct_builders' argument
        in struct_gen. The second list will produce an element of the 'blocks'
        argument in struct_gen.
    id
        The id assigned to this variable's task in the computation.
    symbol_table
        A dict that maps variables to variable names. It is not read by this
        function but a variable name for the variable is computed and added to
        the table.
    sub
        Dictionary for use by L{CodeBlock}.

    """

    name = "V%i" % id
    symbol_table[variable] = name
    sub = dict(sub)
#    sub['name'] = name
    sub['id'] = id
    sub['fail'] = failure_code_init(sub)
    sub['py_ptr'] = "py_%s" % name
    sub['stor_ptr'] = "storage_%s" % name
    # struct_declare, struct_behavior, struct_cleanup, sub)
    struct_builder = CodeBlock(*[apply_policy(policy, variable, name, sub)
                                 for policy in policies[0]] + [sub])
    sub['id'] = id + 1
    sub['fail'] = failure_code(sub)
    sub['py_ptr'] = "py_%s" % name
    sub['stor_ptr'] = "storage_%s" % name
    # run_declare, run_behavior, run_cleanup, sub)
    block = CodeBlock(*[apply_policy(policy, variable, name, sub)
                        for policy in policies[1]] + [sub])

    return struct_builder, block


class CLinker(link.Linker):
    """
    WRITEME

    Creates C code for an fgraph, compiles it and returns callables
    through make_thunk and make_function that make use of the compiled
    code.

    no_recycling can contain a list of Variables that belong to the fgraph.
    If a Variable is in no_recycling, CLinker will clear the output storage
    associated to it during the computation (to avoid reusing it).

    """

    def __init__(self, schedule=None, c_callable=False):
        self.fgraph = None
        if schedule:
            self.schedule = schedule
        self.c_callable = c_callable

    def accept(self, fgraph, no_recycling=None):
        """
        WRITEME

        """
        if no_recycling is None:
            no_recycling = []
        if self.fgraph is not None and self.fgraph is not fgraph:
            return type(self)().accept(fgraph, no_recycling)
            # raise Exception("Cannot accept from a Linker that is already"
            #                " tied to another FunctionGraph.")
        self.fgraph = fgraph
        self.fetch_variables()
        self.no_recycling = no_recycling
        return self

    def fetch_variables(self):
        """
        WRITEME

        Fills the inputs, outputs, variables, orphans, temps and node_order
        fields.

        """
        fgraph = self.fgraph
        self.inputs = fgraph.inputs
        self.outputs = fgraph.outputs

        self.node_order = self.schedule(fgraph)
        
        # list(fgraph.variables)
        # We need to include the unused inputs in our variables,
        # otherwise we can't pass them to the module.
        self.variables = [var for var in self.inputs if not len(var.clients)]
        self.variables += graph.variables(self.inputs, self.outputs)

        # This adds a hidden input which is the context for each node
        # that needs it
        self.contexts = dict()
        for node in self.node_order:
            ctx = node.run_context()
            if ctx is not graph.NoContext:
                # try to avoid creating more than one variable for the
                # same context.
                if ctx in self.contexts:
                    var = self.contexts[ctx]
                    assert var.type == node.context_type
                    var.clients.append((node, 'context'))
                else:
                    var = graph.Constant(node.context_type, ctx)
                    var.clients = [(node, 'context')]
                    self.contexts[ctx] = var
                    self.variables.append(var)

        # The orphans field is listified to ensure a consistent order.
        # list(fgraph.orphans.difference(self.outputs))
        self.orphans = list(r for r in self.variables
                            if isinstance(r, graph.Constant) and
                            r not in self.inputs)
        self.temps = list(set(self.variables).difference(
            self.inputs).difference(self.outputs).difference(self.orphans))
        self.consts = []

    def code_gen(self):
        """
        WRITEME

        Generates code for a struct that does the computation of the fgraph and
        stores it in the struct_code and run_code field of the instance.

        If reuse_storage is True, outputs and temporaries will be stored in
        the struct so they can be reused each time a function returned by
        make_function is called, which means that the output of a call will
        be invalidated by the next. If reuse_storage is False, that problem
        is avoided.

        This method caches its computations.

        """
        if getattr(self, 'struct_code', False):
            return self.struct_code, self.run_code

        no_recycling = self.no_recycling

        self.consts = []
        c_support_code_apply = []
        c_init_code_apply = []

        symbol = {}

        # (init_)tasks contains a list of pairs (Op/Variable, task_name)
        # e.g. (x, 'get') or (x+y, 'code')
        init_tasks = []
        tasks = []

        # (init_)blocks contain CodeBlock instances. There is a direct
        # correspondance with (init_)tasks.
        init_blocks = []
        blocks = []

        failure_var = "__failure"
        id = 1
        for variable in self.variables:
            sub = dict(failure_var=failure_var)
            # it might be possible to inline constant variables as C literals
            # policy = [[what to declare in the struct,
            #            what to do at construction,
            #            what to do at destruction],
            #           [what to declare in each run,
            #            what to do at the beginning of each run,
            #            what to do at the end of each run]]
            if variable in self.inputs:
                # We need to extract the new inputs at each run
                # they do not need to be relayed to Python, so we don't sync.
                # If the variable is both an input and an output, there is
                # no need to synchronize either, it is already up-to-date.
                policy = [[get_nothing, get_nothing, get_nothing],
                          [get_c_declare, get_c_extract, get_c_cleanup]]
            elif variable in self.orphans:
                if not isinstance(variable, graph.Constant):
                    raise TypeError("All orphans to CLinker must be Constant"
                                    " instances.", variable)
                if isinstance(variable, graph.Constant):
                    try:
                        symbol[variable] = ("(" + variable.type.c_literal(
                            variable.data) + ")")
                        self.consts.append(variable)
                        self.orphans.remove(variable)
                        continue
                    except (utils.MethodNotDefined, NotImplementedError):
                        pass
                # orphans are not inputs so we'll just get fetch them
                # when we initialize the struct and assume they stay
                # the same
                policy = [[get_c_declare, get_c_extract, get_c_cleanup],
                          [get_nothing, get_nothing, get_nothing]]
            elif variable in self.temps:
                # temps don't need to be extracted from Python, so we
                # call c_init rather than c_extract they do not need
                # to be relayed to Python, so we don't sync
                if variable.type.c_is_simple() or variable in no_recycling:
                    policy = [[get_nothing, get_nothing, get_nothing],
                              [get_c_declare, get_c_init, get_c_cleanup]]
                else:
                    # it is useful for complex temps to reuse storage
                    # at each run, so we only clean up in the
                    # destructor
                    policy = [[get_c_declare, get_c_init, get_c_cleanup],
                              [get_nothing, get_nothing, get_nothing]]
            elif variable in self.outputs:
                if variable.type.c_is_simple() or variable in no_recycling:
                    # Do not extract output from Python
                    policy = [[get_nothing, get_nothing, get_nothing],
                              [get_c_declare, get_c_init,
                                  (get_c_sync, get_c_cleanup)]]
                else:
                    # We try to use the output that is pre-allocated.
                    # The linker will usually just reuse the storage
                    # from last run, but in the first execution,
                    # it will be None.
                    # We clean-up at each run to enable garbage collection
                    # in the Linker.
                    policy = [[get_nothing, get_nothing, get_nothing],
                              [get_c_declare, get_c_extract_out,
                                  (get_c_sync, get_c_cleanup)]]
            else:
                raise Exception("what the fuck")

            builder, block = struct_variable_codeblocks(variable, policy,
                                                        id, symbol, sub)

            # each Variable generates two CodeBlocks, one to
            # declare/initialize/destroy struct variables and the
            # other to declare/extract/cleanup each time the function
            # is run.
            # Typically, only one of the two actually does anything
            # (see all the possible combinations above)

            init_tasks.append((variable, 'init', id))
            init_blocks.append(builder)

            tasks.append((variable, 'get', id + 1))
            blocks.append(block)

            id += 2

        for node_num, node in enumerate(self.node_order):

            sub = dict(failure_var=failure_var)

            ctx = node.run_context()
            if ctx is not graph.NoContext:
                context_var = symbol[self.contexts[ctx]]

            # The placeholder will be replaced by a hash of the entire
            # code (module + support code) in DynamicModule.code.
            # This ensures that, when defining functions in support code,
            # we cannot have two different functions, in different modules,
            # that have the same name.
            # It was problematic, in particular, on Mac OS X (10.6 and 10.7)
            # when defining CUDA kernels (with Cuda 4.2 and 5.0). See gh-1172.
            name = "node_<<<<HASH_PLACEHOLDER>>>>_%i" % node_num
            isyms = [symbol[r] for r in node.inputs]
            osyms = [symbol[r] for r in node.outputs]

            # Make the CodeBlock for c_code
            sub['id'] = id
            sub['fail'] = failure_code(sub)
            if ctx is not graph.NoContext:
                sub['context'] = context_var

            sub_struct = dict()
            sub_struct['id'] = id + 1
            sub_struct['fail'] = failure_code_init(sub)
            if ctx is not graph.NoContext:
                # Since context inputs are always constants they are
                # guaranteed to be available in the struct init code.
                sub_struct['context'] = context_var

            struct_support = ""
            struct_init = ""
            struct_cleanup = ""

            op = node.op
            # type-specific support code
            try:
                c_support_code_apply.append(op.c_support_code_apply(node,
                                                                    name))
            except utils.MethodNotDefined:
                pass
            else:
                # The following will be executed if the "try" block succeeds
                assert isinstance(c_support_code_apply[-1], string_types), (
                    str(node.op) +
                    " didn't return a string for c_support_code_apply")

            try:
                c_init_code_apply.append(op.c_init_code_apply(node, name))
            except utils.MethodNotDefined:
                pass
            else:
                assert isinstance(c_init_code_apply[-1], string_types), (
                    str(node.op) +
                    " didn't return a string for c_init_code_apply")

            try:
                struct_init = op.c_init_code_struct(node, name, sub_struct)
                assert isinstance(struct_init, string_types), (
                    str(node.op) +
                    " didn't return a string for c_init_code_struct")
            except utils.MethodNotDefined:
                pass

            try:
                struct_support = op.c_support_code_struct(node, name)
                assert isinstance(struct_support, string_types), (
                    str(node.op) +
                    " didn't return a string for c_support_code_struct")
            except utils.MethodNotDefined:
                pass

            try:
                struct_cleanup = op.c_cleanup_code_struct(node, name)
                assert isinstance(struct_cleanup, string_types), (
                    str(node.op) +
                    " didn't return a string for c_cleanup_code_struct")
            except utils.MethodNotDefined:
                pass

            # emit c_code
            try:
                behavior = op.c_code(node, name, isyms, osyms, sub)
            except utils.MethodNotDefined:
                raise NotImplementedError("%s cannot produce C code" % op)
            assert isinstance(behavior, string_types), (
                str(node.op) + " didn't return a string for c_code")
            # To help understand what is following. It help read the c code.
            # This prevent different op that generate the same c code
            # to be merged, I suppose this won't happen...
            behavior = ("// Op class " + node.op.__class__.__name__ + "\n" +
                        behavior)

            try:
                cleanup = op.c_code_cleanup(node, name, isyms, osyms, sub)
            except utils.MethodNotDefined:
                cleanup = ""

            _logger.info('compiling un-versioned Apply %s', str(node))

            blocks.append(CodeBlock("", behavior, cleanup, sub))
            tasks.append((node, 'code', id))
            id += 1

            init_blocks.append(CodeBlock(struct_support, struct_init,
                                         struct_cleanup, {'id': id}))
            init_tasks.append((node, 'init', id))
            id += 1

        # List of arg names for use in struct_gen. Note the call to
        # uniq: duplicate inputs must only be passed once because they
        # are mapped to the same name.  Duplicates are defined by (a
        # is b), rather than (a==b) since Constant instances can
        # compare equal to equivalent Constant instances.
        args = ["storage_%s" % symbol[variable] for variable
                 in utils.uniq(self.inputs + self.outputs + self.orphans)]

        # <<<<HASH_PLACEHOLDER>>>> will be replaced by a hash of the whole
        # code in the file, including support code, in DynamicModule.code.
        struct_name = '__struct_compiled_op_%s' % '<<<<HASH_PLACEHOLDER>>>>'
        struct_code, run_code = struct_gen(args, init_blocks, blocks,
                                 dict(failure_var=failure_var,
                                      name=struct_name), self.c_callable)

        self.struct_code = struct_code
        self.run_code = run_code
        self.struct_name = struct_name
        self.args = args
        self.r2symbol = symbol
        self.init_blocks = init_blocks
        self.init_tasks = init_tasks
        self.blocks = blocks
        self.tasks = tasks
        all_info = self.inputs + self.outputs + self.orphans
        self.c_support_code_apply = c_support_code_apply
        self.c_init_code_apply = c_init_code_apply

        if (self.init_tasks, self.tasks) != self.get_init_tasks():
            print("init_tasks\n", self.init_tasks, file=sys.stderr)
            print(self.get_init_tasks()[0], file=sys.stderr)
            print("tasks\n", self.tasks, file=sys.stderr)
            print(self.get_init_tasks()[1], file=sys.stderr)
            assert (self.init_tasks, self.tasks) == self.get_init_tasks()

        # List of indices that should be ignored when passing the arguments
        # (basically, everything that the previous call to uniq eliminated)
        self.dupidx = [i for i, x in enumerate(all_info)
                       if all_info.count(x) > 1 and all_info.index(x) != i]
        return self.struct_code

    def support_code(self):
        """
        WRITEME

        Returns a list of support code strings that are needed by
        one or more Variables or Ops. The support code from Variables is
        added before the support code from Ops.

        This might contain duplicates.

        """
        ret = []
        # generic support code
        for x in [y.type for y in self.variables] + [
                y.op for y in self.node_order]:
            try:
                ret.append(x.c_support_code())
            except utils.MethodNotDefined:
                pass
        return ret

    def compile_args(self):
        """
        WRITEME

        Returns a list of compile args that are needed by one
        or more Variables or Ops.

        This might contain duplicates.

        """
        ret = ["-O3"]
# this is the param the -ffast-math activate. I put the explicitly as
# FillMissing must disable some of them. Putting -ffast-math would
# make it disable all other parameter at the same time.
        ret += ["-fno-math-errno",
                # "-funsafe-math-optimizations",
                # "-fno-signaling-nans",
                # "-fcx-limited-range",
                # "-fno-rounding-math",
                # "-ffinite-math-only",

                # the current code generate label event if they are not used.
                # Could use gcc attribute for those label only
                "-Wno-unused-label",
                "-Wno-unused-variable",  # idem as the precedent
                "-Wno-write-strings",  # generated by our code generator...
                ]
        for x in [y.type for y in self.variables] + [
                y.op for y in self.node_order]:
            try:
                ret += x.c_compile_args()
            except utils.MethodNotDefined:
                pass

        c_compiler = self.c_compiler()

        ret = utils.uniq(ret)  # to remove duplicate
        # The args set by the compiler include the user flags. We do not want
        # to reorder them
        ret += c_compiler.compile_args()
        for x in [y.type for y in self.variables] + [
                y.op for y in self.node_order]:
            try:
                for i in x.c_no_compile_args():
                    try:
                        ret.remove(i)
                    except ValueError:
                        pass  # in case the value is not there
            except utils.MethodNotDefined:
                pass
        return ret

    def headers(self):
        """
        WRITEME

        Returns a list of headers that are needed by one
        or more Types or Ops.

        The return value will not contain duplicates.

        """
        ret = []
        for x in [y.type for y in self.variables] + [
                y.op for y in self.node_order]:
            try:
                ret += x.c_headers()
            except utils.MethodNotDefined:
                pass
        return utils.uniq(ret)

    def init_code(self):
        """
        Return a list of code snippets that have to be inserted
        in the module initialization code.

        The return value will not contain duplicates.

        """
        ret = []
        for x in [y.type for y in self.variables] + [
                y.op for y in self.node_order]:
            try:
                ret += x.c_init_code()
            except utils.MethodNotDefined:
                pass
        return utils.uniq(ret)

    def c_compiler(self):
        c_compiler = None
        for x in [y.type for y in self.variables] + [
                y.op for y in self.node_order]:
            if hasattr(x, 'c_compiler'):
                x_compiler = x.c_compiler()
            else:
                continue

            if c_compiler is None:
                c_compiler = x_compiler
            else:
                if x_compiler and (x_compiler != c_compiler):
                    raise Exception('Nodes have requested specific'
                                    ' different compilers',
                                    (c_compiler, x_compiler))
        if (c_compiler is None):
            return cmodule.GCC_compiler
        else:
            return c_compiler

    def header_dirs(self):
        """
        WRITEME

        Returns a list of lib directories that are needed by one
        or more Types or Ops.

        The return value will not contain duplicates.

        """
        ret = []
        for x in [y.type for y in self.variables] + [
                y.op for y in self.node_order]:
            try:
                ret += x.c_header_dirs()
            except utils.MethodNotDefined:
                pass
        return utils.uniq(ret)

    def libraries(self):
        """
        WRITEME

        Returns a list of libraries that are needed by one
        or more Types or Ops.

        The return value will not contain duplicates.

        """
        ret = []
        for x in [y.type for y in self.variables] + [
                y.op for y in self.node_order]:
            try:
                ret += x.c_libraries()
            except utils.MethodNotDefined:
                pass
        return utils.uniq(ret)

    def lib_dirs(self):
        """
        WRITEME

        Returns a list of lib directories that are needed by one
        or more Types or Ops.

        The return value will not contain duplicates.

        """
        ret = []
        for x in [y.type for y in self.variables] + [
                y.op for y in self.node_order]:
            try:
                ret += x.c_lib_dirs()
            except utils.MethodNotDefined:
                pass
        return utils.uniq(ret)

    def __compile__(self, input_storage=None, output_storage=None,
                    storage_map=None, keep_lock=False,
                    c_callable=False):
        """WRITEME
        Compiles this linker's fgraph.

        Parameters
        ----------
        input_storage: list or None
            List of lists of length 1. In order to use the thunk returned
            by __compile__, the inputs must be put in that storage.
            If None, storage will be allocated.
        output_storage: list of lists of length 1
            The thunk returned by __compile__ will put the variables of the
            computation in these lists. If None, storage will be allocated.

        Returns
        -------
        object
            Thunk, input_storage, output_storage, error_storage.

        """
        error_storage = [None, None, None]
        if input_storage is None:
            input_storage = tuple([None] for variable in self.inputs)
        if output_storage is None:
            map = {}
            output_storage = []
            # Initialize the map with the inputs, as some outputs may
            # be inputs as well.
            for i, variable in enumerate(self.inputs):
                map[variable] = input_storage[i]
            for variable in self.outputs:
                if variable not in map:
                    map[variable] = [None]
                output_storage.append(map[variable])
        input_storage = tuple(input_storage)
        output_storage = tuple(output_storage)
        thunk, filename = self.cthunk_factory(error_storage,
                                              input_storage,
                                              output_storage,
                                              storage_map,
                                              keep_lock=keep_lock)
        return (thunk,
                [link.Container(input, storage) for input, storage in
                 izip(self.fgraph.inputs, input_storage)],
                [link.Container(output, storage, True) for output, storage in
                 izip(self.fgraph.outputs, output_storage)],
                error_storage,
                filename)

    def get_init_tasks(self):
        init_tasks = []
        tasks = []
        id = 1
        for v in self.variables:
            if v in self.consts:
                continue
            if v in self.orphans and isinstance(v, graph.Constant):
                try:
                    # constant will be inlined, no need to get
                    v.type.c_literal(v.data)
                    continue
                except (utils.MethodNotDefined, NotImplementedError):
                    pass
            init_tasks.append((v, 'init', id))
            tasks.append((v, 'get', id + 1))
            id += 2
        for node in self.node_order:
            tasks.append((node, 'code', id))
            init_tasks.append((node, 'init', id + 1))
            id += 2
        return init_tasks, tasks

    def make_thunk(self, input_storage=None, output_storage=None,
                   storage_map=None, keep_lock=False):
        """WRITEME
        Compiles this linker's fgraph and returns a function to perform the
        computations, as well as lists of storage cells for both the inputs
        and outputs.

        Parameters
        ----------
        input_storage: list or None
            List of lists of length 1. In order to use
            the thunk returned by __compile__, the inputs must be put in
            that storage. If None, storage will be allocated.
        @param output_storage: list of lists of length 1. The thunk returned
            by __compile__ will put the variables of the computation in these
            lists. If None, storage will be allocated.
        @param storage_map: dict that map variables to storages. This is used
            when you need to customize the storage of this thunk.

        Returns: thunk, input_storage, output_storage

        The return values can be used as follows:
          f, istor, ostor = clinker.make_thunk()
          istor[0].data = first_input
          istor[1].data = second_input
          f()
          first_output = ostor[0].data
        """
        init_tasks, tasks = self.get_init_tasks()
        cthunk, in_storage, out_storage, error_storage, filename = self.__compile__(
            input_storage, output_storage, storage_map,
            keep_lock=keep_lock)

        res = _CThunk(cthunk, init_tasks, tasks, error_storage, filename)
        res.nodes = self.node_order
        return res, in_storage, out_storage

    def cmodule_key(self):
        """
        Return a complete hashable signature of the module we compiled.

        This function must have the property that no two programs that
        compute different things yield the same key.

        The key returned by this function is of the form (version, signature)
        The signature has the following form:
        {{{
            'CLinker.cmodule_key', compilation args, libraries,
            header_dirs, numpy ABI version, config md5,
            [c_callable,]
            (op0, input_signature0, output_signature0),
            (op1, input_signature1, output_signature1),
            ...
            (opK, input_signatureK, output_signatureK),
        }}}

        The signature is a tuple, some elements of which are sub-tuples.

        The outer tuple has a brief header, containing the compilation options
        passed to the compiler, the libraries to link against, an md5 hash
        of theano.config (for all config options where "in_c_key" is True).

        If CLinker.c_callable is True, it is added to the signature.
        It is followed by elements for every node in the
        topological ordering of `self.fgraph`.

        If the Op of any Apply in the FunctionGraph does not have
        c_code_cache_ok()==True, then this function raises a KeyError
        exception.

        Input Signature
        ---------------

        Each input signature is a tuple with an element for each input
        to the corresponding Apply node. Each element identifies the
        type of the node input, and the nature of that input in the
        graph.

        The nature of a typical variable is encoded by integer pairs
        ``((a,b),c)``:
        ``a`` is the topological position of the input's owner
              (-1 for graph inputs),
        ``b`` is the index of the variable in the owner's output list.
        ``c`` is a flag indicating whether the variable is in the
              no_recycling set.

        If a variable is also a graph output, then its position in the
        outputs list is also bundled with this tuple (after the b).

        The nature of a Constant instance is defined as its signature,
        together with two integers: the topological position of the
        first Apply using that Constant instance, and the lowest index
        into that Apply's inputs that refers to that Constant.  (These
        two integers are a surrogate for the id() of the Constant.
        The integers are important because merge-able constants have
        the same signature, but require separate containers in C
        code.)  The membership in no_recycling is also included in the
        signature.

        Output Signature
        ----------------

        The outputs of a node are entirely determined by the node's Op
        and the nature of the inputs, but the set of outputs that may
        be re-used by the computation (the elements of
        self.no_recycling) can affect the code that is generated.

        The format of each Op's output signature is a (version, no_recycle)
        pair, where version is incremented if codegen() changes how it
        handles the outputs, and no_recycle is simply a list of
        booleans, indicating whether each output is in the
        no_recycling set. Older versions of compiled modules only have the
        no_recycle list.

        """
        return self.cmodule_key_(self.fgraph, self.no_recycling,
                                 compile_args=self.compile_args(),
                                 libraries=self.libraries(),
                                 header_dirs=self.header_dirs(),
                                 c_compiler=self.c_compiler(),
                                 c_callable=self.c_callable)

    def cmodule_key_(self, fgraph, no_recycling, compile_args=None,
                     libraries=None, header_dirs=None, insert_config_md5=True,
                     c_compiler=None, c_callable=None):
        """
        Do the actual computation of cmodule_key in a static method
        to allow it to be reused in scalar.Composite.__eq__.

        """
        if compile_args is None:
            compile_args = []
        if libraries is None:
            libraries = []
        if header_dirs is None:
            header_dirs = []
        order = self.schedule(fgraph)
        # set of variables that have been computed by nodes we have
        # seen 'so far' in the loop below
        fgraph_computed_set = set()
        fgraph_inputs_dict = dict((i, (-1, pos)) for pos, i in
                                  enumerate(fgraph.inputs))
        constant_ids = dict()
        op_pos = {}  # Apply -> topological position

        # First we put the header, compile_args, library names and config md5
        # into the signature.
        sig = ['CLinker.cmodule_key']  # will be cast to tuple on return
        if compile_args is not None:
            # We must sort it as the order from a set is not guaranteed.
            # In  particular, 2 sets with the same content can give different
            # order depending on the order you put data in it.
            # Sets are used to remove duplicate elements.
            args = sorted(compile_args)
            args = tuple(args)
            sig.append(args)
        if libraries is not None:
            # see comments for compile_args
            args = sorted(libraries)
            args = tuple(args)
            sig.append(args)

        if header_dirs is not None:
            args = sorted(header_dirs)
            args = tuple(args)
            sig.append(args)

        # We must always add the numpy ABI version here as
        # DynamicModule always add the include <numpy/arrayobject.h>
        sig.append('NPY_ABI_VERSION=0x%X' %
                   numpy.core.multiarray._get_ndarray_c_version())
        if c_compiler:
            sig.append('c_compiler_str=' + c_compiler.version_str())

        # IMPORTANT: The 'md5' prefix is used to isolate the compilation
        # parameters from the rest of the key. If you want to add more key
        # elements, they should be before this md5 hash if and only if they
        # can lead to a different compiled file with the same source code.
        if insert_config_md5:
            sig.append('md5:' + theano.configparser.get_config_md5())
        else:
            sig.append('md5: <omitted>')

        # We append it only if we are c_callable to don't trash the
        # old compiled dir.
        if c_callable:
            sig.append('c_callable: ' + str(self.c_callable))
            constants_vals = ""
            for var in self.orphans:
                if isinstance(var, graph.Constant):
                    constants_vals += str(var.data.flatten())
            if constants_vals != '':
                sig.append('constants: '+constants_vals)

        error_on_play = [False]

        def in_sig(i, topological_pos, i_idx):
            # assert that every input to every node is one of'
            # - an fgraph input
            # - an output from a node in the FunctionGraph
            # - a Constant

            # It is important that a variable (i)
            # yield a 'position' that reflects its role in code_gen()
            if isinstance(i, graph.Constant):  # orphans
                if id(i) not in constant_ids:
                    isig = (i.signature(), topological_pos, i_idx)
                    # If the Theano constant provides a strong hash
                    # (no collision for transpose, 2, 1, 0, -1, -2,
                    # 2 element swapped...) we put this hash in the signature
                    # instead of the value. This makes the key file much
                    # smaller for big constant arrays. Before this, we saw key
                    # files up to 80M.
                    if hasattr(isig[0], "theano_hash"):
                        isig = (isig[0].theano_hash(), topological_pos, i_idx)
                    try:
                        hash(isig)
                    except Exception:
                        # generic constants don't have a hashable signature
                        error_on_play[0] = True
                        return None
                    constant_ids[id(i)] = isig
                else:
                    isig = constant_ids[id(i)]
                # print 'SIGNATURE', i.signature()
                # return i.signature()
            elif i in fgraph_inputs_dict:  # inputs
                isig = fgraph_inputs_dict[i]
            else:
                if i.owner is None:
                    assert all(all(out is not None for out in o.outputs)
                               for o in order)
                    assert all(input.owner is None for input in fgraph.inputs)
                    raise Exception('what is this?', (i, type(i), i.clients,
                                                      fgraph))

                if i in fgraph.outputs:
                    isig = (op_pos[i.owner],  # outputs
                            i.owner.outputs.index(i),
                            fgraph.outputs.index(i))
                else:
                    isig = (op_pos[i.owner], i.owner.outputs.index(i))  # temps
            return (isig, i in no_recycling)

        version = []
        for node_pos, node in enumerate(order):
            try:
                # Pure Ops do not have a c_code_cache_version_apply ...
                version.append(node.op.c_code_cache_version_apply(node))
            except AttributeError:
                pass
            for i in node.inputs:
                version.append(i.type.c_code_cache_version())
            for o in node.outputs:
                version.append(o.type.c_code_cache_version())

            # add the signature for this node
            sig.append((
                node.op,
                tuple((i.type, in_sig(i, node_pos, ipos))
                      for ipos, i in enumerate(node.inputs)),
                (1,  # Increment if cmodule change its handling of outputs
                    tuple(o in no_recycling for o in node.outputs))))

            if error_on_play[0]:
                # if one of the signatures is not hashable
                # then bypass the cache mechanism and
                # compile fresh every time
                return None

            op_pos[node] = node_pos
            fgraph_computed_set.update(node.outputs)

        # Add not used input in the key
        for ipos, var in [(i, var) for i, var in enumerate(fgraph.inputs)
                          if not len(var.clients)]:
            sig.append((var.type, in_sig(var, -1, ipos)))

        # crystalize the signature and version
        sig = tuple(sig)
        version = tuple(version)
        for v in version:
            if not v:
                # one of the ops or types here is unversioned,
                # so this fgraph is entirely unversioned
                return ((), sig)
        return version, sig

    def get_src_code(self):
        mod = self.get_dynamic_module()
        return mod.code()

    def compile_cmodule(self, location=None):
        """
        This compiles the source code for this linker and returns a
        loaded module.

        """

        if location is None:
            location = cmodule.dlimport_workdir(config.compiledir)
        mod = self.get_dynamic_module()
        c_compiler = self.c_compiler()
        libs = self.libraries()
        preargs = self.compile_args()
        compiler_name = c_compiler.__name__

        if compiler_name == 'NVCC_compiler' and config.lib.amdlibm:
            # This lib does not work correctly with nvcc in device code.
            # and newer version of g++ as 4.5.1.
            # example of errors: "/usr/lib/gcc/x86_64-redhat-linux/4.5.1/
            #                     include/mmintrin.h(49): error: identifier
            #                     "__builtin_ia32_emms" is undefined"

            if '<amdlibm.h>' in mod.includes:
                mod.includes.remove('<amdlibm.h>')
            if '-DREPLACE_WITH_AMDLIBM' in preargs:
                preargs.remove('-DREPLACE_WITH_AMDLIBM')
            if 'amdlibm' in libs:
                libs.remove('amdlibm')

        if self.c_callable:
            # Add the include filename with the placeholder, as the hash is not
            # yet computed, but we need to add the include to compute the hash.
            filename_h = os.path.join(location, mod.hash_placeholder + '.h')
            mod.add_include(filename_h)
        src_code = mod.code()
        if self.c_callable:
            filename_h = os.path.join(location, '%s.h' % mod.code_hash)
            mod.gen_header(filename_h)
        get_lock()
        try:
            _logger.debug("LOCATION %s", str(location))
            try:
                module = c_compiler.compile_str(
                    module_name=mod.code_hash,
                    src_code=mod.code(),
                    location=location,
                    include_dirs=self.header_dirs(),
                    lib_dirs=self.lib_dirs(),
                    libs=libs,
                    preargs=preargs)

                if self.c_callable:
                    # The main of the executable need the hash of the
                    # shared lib.
                    main = re.sub(mod.hash_placeholder, mod.code_hash,
                                  self.c_main(location))

                    mod_exec = cmodule.DynamicModule()
                    for header in self.headers():
                        mod_exec.add_include(header)
                    mod_exec.add_include(filename_h)
                    mod_exec.add_support_code(main)
                    makefile = c_compiler.make_makefile(
                        module_name=mod.code_hash,
                        location=location,
                        include_dirs=self.header_dirs(),
                        lib_dirs=self.lib_dirs(),
                        libs=libs,
                        preargs=preargs)
                    f = open(os.path.join(location, 'makefile'), 'w')
                    print(makefile, file=f)
                    f.close()
                    # Put the command line in the header code so that
                    # other people know how to recompile the shared lib
                    mod_exec.add_header_code(
                        "//command line used to compile the shared lib: \n" +
                        "//" + ' '.join(
                            c_compiler.compile_command(
                                module_name=mod.code_hash,
                                location=location,
                                include_dirs=self.header_dirs(),
                                lib_dirs=self.lib_dirs(),
                                libs=libs,
                                preargs=preargs)[2]))

                    # Make the executable link to the shared lib.
                    preargs.append(os.path.join(location, mod.code_hash + "." +
                                                cmodule.get_lib_extension()))
                    # Make the executable
                    mod_exec.add_header_code(
                        "//command line used to compile the executable: \n" +
                        "//" + ' '.join(
                            c_compiler.compile_command(
                                module_name=mod_exec.code_hash,
                                location=location,
                                include_dirs=self.header_dirs(),
                                lib_dirs=self.lib_dirs(),
                                libs=libs,
                                preargs=preargs,
                                shared=False, py_module=False,
                                code_filename='exec.cpp',
                                out_filename='exec')[2]))

                    # compile the dynamic python module.
                    src_code = mod_exec.code(executable=True)
                    c_compiler.compile_str(
                        module_name=mod_exec.code_hash,
                        src_code=src_code,
                        location=location,
                        include_dirs=self.header_dirs(),
                        lib_dirs=self.lib_dirs(),
                        libs=libs,
                        preargs=preargs,
                        shared=False, py_module=False,
                        code_filename='exec.cpp',
                        out_filename='exec')
                    mod_exec.gen_header(os.path.join(location, 'exec.h'))
                    # TODO: make c_callable work also for
                    # differnt types of windows OS
                    if sys.platform == "win32" and False:
                        # I don't know why it work now, but
                        # this was needed in the past.
                        # As this is complicated to find how to do
                        # it, I keep it here
                        # just in case.
                        mt = r"C:\Program Files (x86)\Microsoft SDKs\Windows\v7.0A\Bin\mt.exe"
                        pp = [p for p in sys.path
                              if os.path.exists(os.path.join(p, 'python27.dll'))]
                        # Try the first path found. Currently there is 2 of
                        # them that have the same file size.
                        py_dll = os.path.join(pp[-1], "python27.dll")
                        manifest = os.path.join(location, "py_dll.manifest")
                        exec_f = os.path.join(location, "exec.exe")
                        call_subprocess_Popen('"' + mt + '"' +
                                            " -inputresource:" + py_dll + ";#2 -out:" + manifest)
                        call_subprocess_Popen('"' + mt + '"' +
                            " -manifest " + manifest +
                            " -outputresource:" + exec_f)

            except Exception as e:
                e.args += (str(self.fgraph),)
                raise
        finally:
            release_lock()
        return module

    def get_dynamic_module(self):
        """
        Return a cmodule.DynamicModule instance full of the code for our fgraph.

        This method is cached on the first call so it can be called
        multiple times without penalty.

        """
        if not hasattr(self, '_mod'):
            self.code_gen()

            mod = cmodule.DynamicModule()

            # The code of instantiate
            # the 1 is for error_storage
            code = self.instantiate_code(1 + len(self.args))
            instantiate = cmodule.ExtFunction('instantiate', code,
                                              method=cmodule.METH_VARARGS)

            # Static methods that can run and destroy the struct built by
            # instantiate.
            if PY3:
                static = """
        static int {struct_name}_executor({struct_name} *self) {{
            return self->run();
        }}

        static void {struct_name}_destructor(PyObject *capsule) {{
            {struct_name} *self = ({struct_name} *)PyCapsule_GetContext(capsule);
            delete self;
        }}
        """.format(struct_name=self.struct_name)
            else:
                static = """
        static int %(struct_name)s_executor(%(struct_name)s* self) {
            return self->run();
        }

        static void %(struct_name)s_destructor(void* executor, void* self) {
            delete ((%(struct_name)s*)self);
        }
        """ % dict(struct_name=self.struct_name)

        # We add all the support code, compile args, headers and libs we need.
                for support_code in self.support_code() + self.c_support_code_apply:
                    mod.add_support_code(support_code)
                    if not self.c_callable:
                        mod.add_support_code("""
                                             #ifdef _WIN32
                                             #define DllExport __declspec(dllexport)
                                             #else
                                             #define DllExport
                                             #endif
                                             """)
                        mod.add_support_code(self.struct_code)
                    else:
                        mod.add_header_code("""
                                            #ifdef _WIN32
                                            #define DllExport __declspec(dllexport)
                                            #else
                                            #define DllExport
                                            #endif
                                            """)
                        mod.add_header_code(self.struct_code)
                mod.add_support_code(self.run_code)
                mod.add_support_code(static)
                mod.add_function(instantiate)
                for header in self.headers():
                    mod.add_include(header)
                for init_code_block in self.init_code() + self.c_init_code_apply:
                    if self.c_callable:
                        mod.add_support_code(self.cinit_code())
                        mod.add_header_code("""
                                            DllExport %(struct_name)s* cinit();
                                            """ % dict(struct_name=self.struct_name))
                    mod.add_init_code(init_code_block)
                self._mod = mod
        return self._mod

    def cthunk_factory(self, error_storage, in_storage, out_storage,
                       storage_map=None, keep_lock=False):
        """WRITEME
        error_storage -> list of length 3
        in_storage -> list of lists of length 1, one per input
        out_storage -> list of lists of length 1, one per output

        Returns a thunk that points to an instance of a C struct that
        can carry on the computation of this linker's fgraph. That thunk,
        when executed, will fetch its inputs from in_storage, put its
        outputs in out_storage and if an error occurs will put the
        type, value and traceback of the exception in error_storage.
        """
        try:
            key = self.cmodule_key()
        except KeyError:
            key = None

        if key is None:  # or self.c_callable is True:
            # If we can't get a key, then forget the cache mechanism.
            module = self.compile_cmodule()
        else:
            module = get_module_cache().module_from_key(
                key=key, lnk=self, keep_lock=keep_lock)

        vars = self.inputs + self.outputs + self.orphans
        # List of indices that should be ignored when passing the arguments
        # (basically, everything that the previous call to uniq eliminated)
        dupidx = [i for i, x in enumerate(vars)
                  if vars.count(x) > 1 and vars.index(x) != i]

        out_storage = [x for i, x in enumerate(out_storage)
                       if (i + len(in_storage)) not in dupidx]
        in_storage = [x for i, x in enumerate(in_storage) if i not in dupidx]
        if storage_map is None:
            orphd = [[orphan.data] for orphan in self.orphans]
        else:
            orphd = [storage_map[orphan] for orphan in self.orphans]

        ret = module.instantiate(error_storage,
                                 *(in_storage + out_storage + orphd))

        return ret, module.__file__

    def instantiate_code(self, n_args):
        code = StringIO()
        struct_name = self.struct_name
        print("static PyObject * instantiate(PyObject * self, PyObject *argtuple) {", file=code)
        print('  assert(PyTuple_Check(argtuple));', file=code)
        print('  if (%(n_args)i != PyTuple_Size(argtuple)){ ' % locals(), file=code)
        print('     PyErr_Format(PyExc_TypeError, "Wrong number of arguments, expected %(n_args)i, got %%i", (int)PyTuple_Size(argtuple));' % locals(), file=code)
        print('     return NULL;', file=code)
        print('  }', file=code)
        print('  %(struct_name)s* struct_ptr = new %(struct_name)s();' % locals(), file=code)
        print('  if (struct_ptr->init(', ','.join('PyTuple_GET_ITEM(argtuple, %i)' % n for n in xrange(n_args)), ') != 0) {', file=code)
        print('    delete struct_ptr;', file=code)
        print('    return NULL;', file=code)
        print('  }', file=code)
        if PY3:
            print("""\
    PyObject* thunk = PyCapsule_New((void*)(&{struct_name}_executor), NULL, {struct_name}_destructor);
    if (thunk != NULL && PyCapsule_SetContext(thunk, struct_ptr) != 0) {{
        PyErr_Clear();
        Py_DECREF(thunk);
        thunk = NULL;
    }}
""".format(**locals()), file=code)
        else:
            print('  PyObject* thunk = PyCObject_FromVoidPtrAndDesc((void*)(&%(struct_name)s_executor), struct_ptr, %(struct_name)s_destructor);' % locals(), file=code)
        print("  return thunk; }", file=code)
        return code.getvalue()

    def c_main(self, location):
        """This function create an example main function that call the
        shared lib of this thunk/function.

        """
        in_init = ""
        out_print = ""
        args = ["storage_%s" % self.r2symbol[variable] for variable
                in utils.uniq(self.inputs)]
        mapping_str = "// Mapping: variable name -> struct internal stogare\n"
        for var, name in zip(utils.uniq(self.inputs), args):
            if isinstance(var, theano.tensor.sharedvar.TensorSharedVariable):
                mapping_str += "// Shared variable %(var)s->%(name)s\n" \
                               % locals()
                # Saving shared variable value
                numpy.save(os.path.join(location, name), var.get_value())
            else:
                mapping_str += "// Input %(var)s->%(name)s\n" % locals()
            dtype = var.type.dtype_specs()[2]
            ndim = var.ndim
            shp = range(3, 3+ndim)
            for idx in range(var.ndim):
                if var.type.broadcastable[idx]:
                    shp[idx] = 1
            tot = numpy.prod(shp)
            shp_str = ",".join([str(s) for s in shp])
            in_init += """
   PyObject* %(name)s_data = PyArray_Arange(0., %(tot)s, 1.,%(dtype)s);
   npy_intp %(name)s_dims[%(ndim)s] = {%(shp_str)s};
   PyArray_Dims %(name)s_newshape;
   %(name)s_newshape.ptr = %(name)s_dims;
   %(name)s_newshape.len = %(ndim)s;
   PyObject* %(name)s_value = PyArray_Newshape(
   (PyArrayObject*) %(name)s_data,&%(name)s_newshape,NPY_CORDER);
   PyList_SetItem(struct_ptr->%(name)s, 0, %(name)s_value);
   %(name)s_value = NULL;
            """ % locals()
        args = ["storage_%s" % self.r2symbol[variable] for variable
                in utils.uniq(self.outputs)]
        for var, name in zip(utils.uniq(self.outputs), args):
            mapping_str += "// Output %(var)s->%(name)s\n" % locals()
        if sys.platform != "win32":
            for name in args:
                out_print += """
                //PyList_GET_ITEM return a borrowed reference
                PyObject *tmp_%(name)s=PyList_GET_ITEM(struct_ptr->%(name)s, 0);
                PyObject_Print(tmp_%(name)s, stdout, Py_PRINT_RAW);
                printf("\\n");
                """ % locals()
        else:
            # On Windows, when the python is compiled with a different run time
            # (Visual Studio version?), then the executable, the stdout and
            # stderr shouldn't not be shared between the 2 run time, so we
            # can't call PyObject_Print(). Otherwise, it segfault.
            for out, name in zip(self.outputs, args):
                out_print += """
   //PyList_GET_ITEM return a borrowed reference
   PyObject *tmp_%(name)s=PyList_GET_ITEM(struct_ptr->%(name)s, 0);
   PyObject *str_%(name)s = PyObject_Str(tmp_%(name)s);
   //PyString_AsString return a ptr to the internal representation.
   printf("%%s\\n", PyString_AsString(str_%(name)s));
   Py_CLEAR(str_%(name)s);
                """ % locals()
        main = """
%(mapping_str)s
int main(int argc, char *argv[]) {
 Py_SetProgramName(argv[0]);  /* optional but recommended */
 Py_Initialize();

 // Those print are there to help debug import of python module
 PyObject * numpy = PyImport_ImportModule("numpy");
 printf("After import numpy %%p\\n", numpy);
 PyErr_Print();
 //import_array{,1,2} can be called many times without problems.
 import_array1(1);
 printf("after import_array1()\\n");

 %(struct_name)s *struct_ptr = cinit();
 int run_ret = 0;
 if(struct_ptr){
   %(in_init)s
   printf("after cinit()\\n");
   //Function execution
   run_ret = struct_ptr->run();
   printf("run() from the shared library returned=%%d\\n", run_ret);

   if(run_ret==0){
   %(out_print)s
   }else if(run_ret != 0){
     // See out_print to know why we can't call PyObject_Print on win32
     PyObject *str_err = PyObject_Str(struct_ptr->__ERROR);
     //PyString_AsString return a ptr to the internal representation.
     printf("Error: %%s\\n", PyString_AsString(str_err));
     Py_CLEAR(str_err);
   }
 }else{
   printf("cinit() failed!\\n");
   return 1;
 }
 delete struct_ptr;

 printf("main end, before Py_Finalize\\n");
 Py_Finalize();
 return run_ret;
}
        """ % dict(struct_name=self.struct_name,
                   **locals())
        # TODO, should struct_ptr.cleanup() cleanup the __ERROR structure
        return main

    def cinit_code(self):
        code = StringIO()
        n_args = len(self.args)
        struct_name = self.struct_name
        param = ','.join('PyObject * arg_%i' % n for n in range(n_args)), ');'
        symbol2r = {v: k for k, v in self.r2symbol.items()}
        in_out_list = ""
        in_out_param = ["io%d_list" % idx for idx in range(n_args)]
        for idx, arg in enumerate(self.args):
            var = in_out_param[idx]
            argType = symbol2r[arg.replace("storage_", "")]
            if isinstance(argType, graph.Constant):
                c_constant_type = argType.type.dtype_specs()[1]
                numpy_constant_type = argType.type.dtype_specs()[2]
                ndim = argType.type.ndim
                vals = argType.data.flatten()
                shp = argType.data.shape
                nelements = vals.shape[0]
                shp_str = ",".join([str(s) for s in shp])
                for_str = ""
                for i, el in enumerate(vals):
                    for_str += "ptr_%(var)s[%(i)s]=%(el)s;\n"%locals()

                in_out_list += """
                PyObject* %(var)s = PyList_New(1);
                npy_intp %(var)s_dims[%(ndim)s]={%(shp_str)s};
                PyObject* const_%(var)s = PyArray_ZEROS(%(ndim)s,%(var)s_dims,%(numpy_constant_type)s,0);
                %(c_constant_type)s *ptr_%(var)s =(%(c_constant_type)s *)PyArray_DATA((PyArrayObject*)const_%(var)s);
                %(for_str)s
                Py_XINCREF(const_%(var)s);
                PyList_SetItem(%(var)s, 0,const_%(var)s);
                """ % locals()
            else:
                in_out_list += """
                PyObject* %(var)s = PyList_New(1);
                Py_INCREF(Py_None);
                PyList_SetItem(%(var)s, 0, Py_None);
                """ % locals()
        in_out_param = ', '.join(in_out_param)
        print("""
%(struct_name)s* cinit() {

    import_array1(NULL);

    // Define the list for error information.
    PyObject* err_list = PyList_New(3);
    PyList_SetItem(err_list, 0, Py_None);
    PyList_SetItem(err_list, 1, Py_None);
    PyList_SetItem(err_list, 2, Py_None);
    Py_INCREF(Py_None);
    Py_INCREF(Py_None);
    Py_INCREF(Py_None);

    %(in_out_list)s

    //TODO error handling
    %(struct_name)s* struct_ptr = new %(struct_name)s();

    //TODO error handling
    struct_ptr->init(err_list, %(in_out_param)s);
    return struct_ptr;
}
        """ % locals(), file=code)

        return code.getvalue()


class _CThunk(object):
    """
    A thunk with a C implementation.

    Parameters
    ----------
    cthunk
        The CObject pointer used by run_cthunk.
    init_tasks
        WRITEME
    tasks
        WRITEME
    error_storage
        WRITEME

    """

    def __init__(self, cthunk, init_tasks, tasks, error_storage, filename):
        """
        Parameters
        ----------
        cthunk: the CObject pointer used by run_cthunk
        init_tasks: WRITEME
        tasks: WRITEME
        error_storage: WRITEME
        filename: str
                  the name of the dynamic lib
                  where this thunk is compiled.
        """
        global run_cthunk
        if run_cthunk is None:
            # Lazy import to avoid compilation when importing theano.
            from theano.gof.cutils import run_cthunk  # noqa
        self.cthunk = cthunk
        self.init_tasks = init_tasks
        self.tasks = tasks
        self.error_storage = error_storage
        self.filename = filename

    def find_task(self, failure_code):
        """
        Maps a failure code to the task that is associated to it.

        """
        failure_code -= 1
        n = len(self.init_tasks)
        # note that the failure code is distributed in two lists
        if failure_code < 2 * n:
            return [self.init_tasks, self.tasks][
                failure_code % 2][failure_code // 2]
        else:
            return self.tasks[failure_code - n]

    def __call__(self):
        failure = run_cthunk(self.cthunk)
        if failure:
            task, taskname, id = self.find_task(failure)
            try:
                trace = task.trace
            except AttributeError:
                trace = ()
            try:
                exc_type, _exc_value, exc_trace = self.error_storage
                if task in self.nodes:
                    self.position_of_error = self.nodes.index(task)
                # this can be used to retrieve the location the Op was declared
                exc_value = exc_type(_exc_value)
                exc_value.__thunk_trace__ = trace
            except Exception:
                print(('ERROR retrieving error_storage.'
                       'Was the error set in the c code?'),
                      end=' ', file=sys.stderr)
                print(self.error_storage, file=sys.stderr)
                raise
            reraise(exc_type, exc_value, exc_trace)


class OpWiseCLinker(link.LocalLinker):
    """
    WRITEME

    Uses CLinker on the individual Ops that comprise an fgraph and loops
    over them in Python. The variable is slower than a compiled version of
    the whole fgraph, but saves on compilation time because small changes
    in the computation graph won't necessarily trigger any recompilation,
    only local changes in the Variables or Ops that are used.

    If fallback_on_perform is True, OpWiseCLinker will use an op's
    perform method if no C version can be generated.

    no_recycling can contain a list of Variables that belong to the fgraph.
    If a Variable is in no_recycling, CLinker will clear the output storage
    associated to it prior to computation (to avoid reusing it).

    Notes
    -----
    This is in a sense the 'default' linker for Theano. The
    overhead of using the OpWiseCLinker as compared with the CLinker
    is only noticeable for graphs of very small tensors (such as 20
    elements or less).

    """

    __cache__ = {}

    def __init__(self,
                 fallback_on_perform=True,
                 allow_gc=None,
                 nice_errors=True,
                 schedule=None):
        if allow_gc is None:
            allow_gc = config.allow_gc
        self.fgraph = None
        self.fallback_on_perform = fallback_on_perform
        self.nice_errors = nice_errors
        self.allow_gc = allow_gc
        if schedule:
            self.schedule = schedule

    def accept(self, fgraph, no_recycling=None):
        if no_recycling is None:
            no_recycling = []
        if self.fgraph is not None and self.fgraph is not fgraph:
            return type(self)(
                fallback_on_perform=self.fallback_on_perform,
                allow_gc=self.allow_gc,
                nice_errors=self.nice_errors
            ).accept(fgraph, no_recycling)
            # raise Exception("Cannot accept from a Linker that is
            # already tied to another FunctionGraph.")
        self.fgraph = fgraph
        self.no_recycling = no_recycling
        return self

    def make_all(self, profiler=None, input_storage=None, output_storage=None,
                 storage_map=None):

        # The lock will be acquired when we compile the first
        # C code. We will keep the lock untill all the function
        # compilation will be finished. This allow to don't
        # require the lock when all c code are already compiled!
        orig_n_lock = getattr(get_lock, "n_lock", 0)
        try:

            fgraph = self.fgraph
            order = self.schedule(fgraph)
            no_recycling = self.no_recycling

            input_storage, output_storage, storage_map = link.map_storage(
                fgraph, order, input_storage, output_storage, storage_map)
            if self.allow_gc:
                computed, last_user = link.gc_helper(order)
                post_thunk_old_storage = []
            else:
                post_thunk_old_storage = None

            compute_map = {}
            for k in storage_map:
                compute_map[k] = [k.owner is None]

            thunks = []
            for node in order:
                # Maker sure we use the C version of the code whenever
                # possible
                # There are ops that don't have _op_use_c_code property
                # for example ifelse (or any ops that come with their own
                # make_thunk
                old_value = getattr(node.op, '_op_use_c_code', False)
                try:
                    if theano.config.cxx:
                        node.op._op_use_c_code = True
                    thunks += [node.op.make_thunk(node,
                                                  storage_map,
                                                  compute_map,
                                                  no_recycling)]
                    thunks[-1].inputs = [storage_map[v] for v in node.inputs]
                    thunks[-1].outputs = [storage_map[v] for v in node.outputs]

                finally:
                    node.op._op_use_c_code = old_value

            for node in order:
                if self.allow_gc:
                    post_thunk_old_storage.append(
                        [storage_map[input] for input in node.inputs
                         if ((input in computed) and
                             (input not in fgraph.outputs) and
                             node == last_user[input])])

            if no_recycling is True:
                no_recycling = list(storage_map.values())
                no_recycling = utils.difference(no_recycling, input_storage)
            else:
                no_recycling = [storage_map[r]
                                for r in no_recycling if r not in fgraph.inputs]

            f = link.streamline(fgraph, thunks, order,
                                post_thunk_old_storage,
                                no_recycling=no_recycling,
                                nice_errors=self.nice_errors)

            f.allow_gc = self.allow_gc

        finally:
            # Release lock on compilation directory.
            if getattr(get_lock, "n_lock", 0) > orig_n_lock:
                release_lock()
                assert get_lock.n_lock == orig_n_lock

        return (f,
                [link.Container(input, storage)
                 for input, storage in izip(fgraph.inputs, input_storage)],
                [link.Container(output, storage, True)
                 for output, storage in izip(fgraph.outputs, output_storage)],
                thunks,
                order)


def _default_checker(x, y):
    """
    WRITEME

    Default checker for DualLinker. This checks that the
    variables contain the same data using ==.

    """
    if x[0] != y[0]:
        raise Exception("Output mismatch.",
                        {'performlinker': x[0], 'clinker': y[0]})


class DualLinker(link.Linker):
    """
    WRITEME

    Runs the fgraph in parallel using PerformLinker and CLinker.

    The thunk/function produced by DualLinker uses PerformLinker as the
    "main" implementation: the inputs and outputs are fed to/taken from
    the Ops' perform. However, DualLinker also instantiates a copy of
    the fgraph on which it runs OpWiseCLinker. At each step, the variables
    of perform and of the C implementation are verified using a checker
    function.

    """

    def __init__(self, checker=_default_checker, schedule=None):
        """
        Initialize a DualLinker.

        The checker argument must be a function that takes two lists
        of length 1. The first one passed will contain the output
        computed by PerformLinker and the second one the output
        computed by OpWiseCLinker. The checker should compare the data
        fields of the two variables to see if they match. By default,
        DualLinker uses ==. A custom checker can be provided to
        compare up to a certain error tolerance.

        If a mismatch occurs, the checker should raise an exception to
        halt the computation. If it does not, the computation will
        carry on and errors will snowball. The checker can sidestep
        the problem by fiddling with the data, but it should be
        careful not to share data between the two outputs (or inplace
        operations that use them will interfere).

        no_recycling can contain a list of Variables that belong to the fgraph.
        If a Variable is in no_recycling, CLinker will clear the output storage
        associated to it during the computation (to avoid reusing it).

        """
        self.fgraph = None
        self.checker = checker
        if schedule:
            self.schedule = schedule

    def accept(self, fgraph, no_recycling=None):
        if no_recycling is None:
            no_recycling = []
        if self.fgraph is not None and self.fgraph is not fgraph:
            return type(self)(self.checker).accept(fgraph, no_recycling)
        self.fgraph = fgraph
        self.no_recycling = no_recycling
        return self

    def make_thunk(self, **kwargs):

        fgraph = self.fgraph
        no_recycling = self.no_recycling

        _f, i1, o1, thunks1, order1 = (
            link.PerformLinker(schedule=self.schedule).accept(
                fgraph, no_recycling=no_recycling).make_all(**kwargs))
        kwargs.pop('input_storage', None)
        _f, i2, o2, thunks2, order2 = (
            OpWiseCLinker(schedule=self.schedule).accept(
                fgraph, no_recycling=no_recycling).make_all(**kwargs))

        def f():
            for input1, input2 in izip(i1, i2):
                # Set the inputs to be the same in both branches.
                # The copy is necessary in order for inplace ops not to
                # interfere.
                input2.storage[0] = copy(input1.storage[0])
            for thunk1, thunk2, node1, node2 in izip(thunks1, thunks2,
                                                     order1, order2):
                for output, storage in izip(node1.outputs, thunk1.outputs):
                    if output in no_recycling:
                        storage[0] = None
                for output, storage in izip(node2.outputs, thunk2.outputs):
                    if output in no_recycling:
                        storage[0] = None
                try:
                    thunk1()
                    thunk2()
                    for output1, output2 in izip(thunk1.outputs,
                                                 thunk2.outputs):
                        self.checker(output1, output2)
                except Exception:
                    link.raise_with_op(node1)

        return f, i1, o1


class HideC(object):
    def __hide(*args):
        raise utils.MethodNotDefined()

    c_code = __hide
    c_code_cleanup = __hide

    c_headers = __hide
    c_header_dirs = __hide
    c_libraries = __hide
    c_lib_dirs = __hide

    c_support_code = __hide
    c_support_code_apply = __hide

    c_compile_args = __hide
    c_no_compile_args = __hide
    c_init_code = __hide
    c_init_code_apply = __hide

    c_init_code_struct = __hide
    c_support_code_struct = __hide
    c_cleanup_code_struct = __hide

    def c_code_cache_version(self):
        return ()

    def c_code_cache_version_apply(self, node):
        return self.c_code_cache_version()
