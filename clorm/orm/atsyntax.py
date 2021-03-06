# -----------------------------------------------------------------------------
# Provides classes and functions to make it easier to specify the integration for
# calling Python from within ASP using the '@' syntax.
# ------------------------------------------------------------------------------

#import logging
#import os
import io
import contextlib
import inspect
import operator
import collections.abc as cabc
import bisect
import functools
import itertools
import clingo
import typing
import re

from .core import *
from .core import get_field_definition

__all__ = [
    'TypeCastSignature',
    'ContextBuilder',
    'make_function_asp_callable',
    'make_method_asp_callable',
    ]

#------------------------------------------------------------------------------
# Global
#------------------------------------------------------------------------------

#------------------------------------------------------------------------------
#------------------------------------------------------------------------------

#------------------------------------------------------------------------------
# When calling Python functions from ASP you need to do some type
# conversions. The TypeCastSignature class can be used to generate a wrapper
# function that does the type conversion for you.
# ------------------------------------------------------------------------------

class TypeCastSignature(object):
    r"""Defines a signature for converting to/from Clingo data types.

    Args:
      sigs(\*sigs): A list of signature elements.

      - Inputs. Match the sub-elements [:-1] define the input signature while
        the last element defines the output signature. Each input must be a a
        RawField (or sub-class).

      - Output: Must be RawField (or sub-class) or a singleton list
        containing a RawField (or sub-class).

   Example:
       .. code-block:: python

           import datetime

           class DateField(StringField):
                     pytocl = lambda dt: dt.strftime("%Y%m%d")
                     cltopy = lambda s: datetime.datetime.strptime(s,"%Y%m%d").date()

           drsig = TypeCastSignature(DateField, DateField, [DateField])

           @drsig.make_clingo_wrapper
           def date_range(start, end):
               return [ start + timedelta(days=x) for x in range(0,end-start) ]

       The function ``date_range`` that takes a start and end date and returns
       the list of dates within that range.

       When *decorated* with the signature it provides the conversion code so
       that the decorated function expects a start and end date encoded as
       Clingo.String objects (matching YYYYMMDD format) and returns a list of
       Clingo.String objects corresponding to the dates in that range.

        """

    @staticmethod
    def _is_input_element(se):
        """An input element must be a subclass of RawField (or an instance of a
           subclass) or a tuple corresponding to a subclass of RawField"""
        return inspect.isclass(se) and issubclass(se, RawField)

    @staticmethod
    def is_return_element(se):
        """An output element must be a subclass of RawField or a singleton containing"""
        if isinstance(se, cabc.Iterable):
            if len(se) != 1: return False
            return TypeCastSignature._is_input_element(se[0])
        return TypeCastSignature._is_input_element(se)

    def __init__(self, *sigs):
        def _validate_basic_sig(sig):
            if TypeCastSignature._is_input_element(sig): return True
            raise TypeError(("TypeCastSignature element {} must be a RawField "
                             "subclass".format(sig)))

        self._insigs = [ type(get_field_definition(s)) for s in sigs[:-1]]
#        self._insigs = sigs[:-1]
        self._outsig = sigs[-1]

        # A tuple is a special case that we want to convert into a complex field
        if isinstance(self._outsig, tuple):
            self._outsig = type(get_field_definition(self._outsig))
        elif isinstance(self._outsig, cabc.Iterable):
            if len(self._outsig) != 1:
                raise TypeError("Return value list signature not a singleton")
            if isinstance(self._outsig[0], tuple):
                self._outsig[0] = type(get_field_definition(self._outsig[0]))

        # Validate the signature
        for s in self._insigs: _validate_basic_sig(s)
        if isinstance(self._outsig, cabc.Iterable):
            _validate_basic_sig(self._outsig[0])
        else:
            _validate_basic_sig(self._outsig)

        # Turn the signature into a tuple
        self._insigs = tuple(self._insigs)

    def _input(self, sig, arg):
        return sig.cltopy(arg)

    def _output(self, sig, arg):
        # Since signature already validated we can make assumptions
        if inspect.isclass(sig) and issubclass(sig, RawField):
            return sig.pytocl(arg)

        # Deal with a list
        if isinstance(sig, cabc.Iterable) and isinstance(arg, cabc.Iterable):
            return [ self._output(sig[0], v) for v in arg ]
        raise ValueError("Value {} does not match signature {}".format(arg, sig))

    @property
    def input_signature(self): return self._insigs

    def wrap_function(self, fn):
        """Function wrapper that adds data type conversions for wrapped function.

        Args:
           fn: A function satisfing the inputs and output defined by the TypeCastSignature.
        """

        @functools.wraps(fn)
        def wrapper(*args):
            if len(args) > len(self._insigs):
                raise ValueError("Mis-matched arguments in call of clingo wrapper")
            newargs = [ self._input(self._insigs[i], arg) for i,arg in enumerate(args) ]
            return self._output(self._outsig, fn(*newargs))
        return wrapper


    def wrap_method(self, fn):
        """Member function wrapper that adds data type conversions for wrapped member
        functions.

        Args:
           fn: A function satisfing the inputs and output defined by the TypeCastSignature.

        """

        @functools.wraps(fn)
        def wrapper(self_, *args):
            if len(args) > len(self._insigs):
                raise ValueError("Mis-matched arguments in call of clingo wrapper")
            newargs = [ self._input(self._insigs[i], arg) for i,arg in enumerate(args) ]
            return self._output(self._outsig, fn(self_, *newargs))
        return wrapper

    def __str__(self):
        insigstr=", ".join([str(s) for s in self._insigs])
        return "{} -> {}".format(insigstr, self._outsig)

    def __repr__(self):
        return self.__str__()

#------------------------------------------------------------------------------
# return and check that function has complete signature
# annotations. ignore_first is useful when dealing with member functions.
#------------------------------------------------------------------------------

def _get_annotations(fn, ignore_first=False):
    fsig = inspect.signature(fn)
    qname = fn.__qualname__
    fsigparam = fsig.parameters
    annotations = [fsigparam[s].annotation for s in fsigparam]
    if not annotations and ignore_first:
        raise TypeError(("Cannot ignore the first parameter for a function "
                         "with no arguments: {}").format(qname))

    # Make sure the return value is annotated
    if inspect.Signature.empty == fsig.return_annotation:
        raise TypeError(("Missing function return annotation: "
                         "{}").format(qname))

    # Remove any ignore first and add the return value annotation
    if ignore_first: annotations.pop(0)
    annotations.append(fsig.return_annotation)

    if inspect.Signature.empty in annotations:
        raise TypeError(("Missing type cast annotations in function "
                         "arguments: {} ").format(qname))
    return annotations


#------------------------------------------------------------------------------
#------------------------------------------------------------------------------

def make_function_asp_callable(*args):
    r"""A decorator for making a function callable from within an ASP program.

    Can be called in a number of ways. Can be called as a decorator with or
    without arguments. If called with arguments then the arguments must
    correspond to a *type cast signature*.

    A *type cast signature* specifies the type conversions required between a
    python function that is called from within an ASP program and a set of
    corresponding Python types.

    A type cast signature is specified in terms of the fields that are used to
    define a predicate.  It is a list of elements where the first n-1 elements
    correspond to type conversions for a functions inputs and the last element
    corresponds to the type conversion for a functions output.

    Args:
      sigs(\*sigs): A list of function signature elements.

      - Inputs. Match the sub-elements [:-1] define the input signature while
        the last element defines the output signature. Each input must be a a
        RawField (or sub-class).

      - Output: Must be RawField (or sub-class) or a singleton list
        containing a RawField (or sub-class).

    If no arguments are provided then the function signature is derived from the
    function annotations. The function annotations must conform to the signature
    above.

    If called as a normal function with arguments then the last element must be
    the function to be wrapped and the previous elements conform to the
    signature profile.

    """
    if len(args) == 0: raise ValueError("Invalid call to decorator")
    fn = None ; sigs = None

    # If the last element is not a function to be wrapped then a signature has
    # been specified.
    if TypeCastSignature.is_return_element(args[-1]):
        sigs = args
    else:
        # Last element needs to be a function
        fn = args[-1]
        if not callable(fn): raise ValueError("Invalid call to decorator")

        # if exactly one element then use function annonations
        if len(args) == 1:
            sigs = _get_annotations(fn)
        else:
            sigs = args[:-1]

    # A decorator function that adjusts for the given signature
    def _sig_decorate(func):
        s = TypeCastSignature(*sigs)
        return s.wrap_function(func)

    # If no function and sig then called as a decorator with arguments
    if not fn and sigs: return _sig_decorate

    return _sig_decorate(fn)

#------------------------------------------------------------------------------
#------------------------------------------------------------------------------

def make_method_asp_callable(*args):
    """A decorator for making a member function callable from within an ASP program.

    See ``make_function_asp_callable`` for details. The only difference is that
    the first element of the function is ignore as it is assumed to be the
    ``self`` or ``cls`` parameter.

    """
    if len(args) == 0: raise ValueError("Invalid call to decorator")
    fn = None ; sigs = None

    # If the last element is not a function to be wrapped then a signature has
    # been specified.
    if TypeCastSignature.is_return_element(args[-1]):
        sigs = args
    else:
        # Last element needs to be a function
        fn = args[-1]
        if not callable(fn): raise ValueError("Invalid call to decorator")

        # if exactly one element then use function annonations
        if len(args) == 1:
            sigs = _get_annotations(fn,True)
        else:
            sigs = args[:-1]

    # A decorator function that adjusts for the given signature
    def _sig_decorate(func):
        s = TypeCastSignature(*sigs)
        return s.wrap_method(func)

    # If no function and sig then called as a decorator with arguments
    if not fn and sigs: return _sig_decorate

    return _sig_decorate(fn)


#------------------------------------------------------------------------------
# Clingo 5.4 introduces the idea of a context to the grounding process. We want
# to make it easier to use this idea. In particular providing a builder with a
# decorator for capturing functions within a context.
# ------------------------------------------------------------------------------
def _context_wrapper(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        return fn(*args, **kwargs)
    return wrapper

class ContextBuilder(object):
    """Context builder simplifies the task of building grounding context for
    clingo. This is a new clingo feature for Clingo 5.4 where a context can be
    provided to the grounding function. The context encapsulates the external
    Python functions that can be called from within an ASP program.

    ``ContextBuilder`` allows arbitrary functions to be captured within a context
    and assigned a conversion signature. It also allows the function to be given
    a different name when called from within the context.

    The context builder's ``register`` and ``register_name`` member functions
    can be called as decorators or as normal functions. A useful feature of
    these functions is that when called as decorators they do not wrap the
    original function but instead return the original function and only wrap the
    function when called from within the context. This is unlike the
    ``make_function_asp_callable`` and ``make_method_asp_callable`` functions
    which when called as decorators will replace the original function with the
    wrapped version.

    Example:

    The following nonsense ASP program contains embedded python with functions
    registered with the context builder (highlighting different ways the
    register functions can be called). A context object is then created by the
    context builder and used during grounding. It will produce the answer set:

       .. code-block:: prolog

          f(5), g(6), h("abcd").

       .. code-block:: python

           f(@addi(1,4)).
           g(@addi_alt(2,4)).
           h(@adds("ab","cd")).

           #script(python).

           from clorm import IntegerField,StringField,ContextBuilder

           IF=IntegerField
           SF=StringField
           cb=ContextBuilder()

           # Uses the function annotation to define the conversion signature
           @cb.register
           def addi(a : IF, b : IF) -> IF : return a+b

           # Register with a different name
           @cb.register_name("addi_alt")
           def add2(a : IF, b : IF) -> IF : return a+b

           # Register with a different name and override the signature in the
           # function annotation
           cb.register_name("adds", SF, SF, SF, addi)

           ctx=cb.make_context()

           def main(prg):
               prg.ground([("base",[])],context=ctx)
               prg.solve()

           #end.

    """

    def __init__(self):
        self._funcs = {}

    def _add_function(self, name, sig, fn):
        if name in self._funcs:
            raise ValueError(("Function name '{}' has already been "
                              "used").format(name))
        self._funcs[name]=_context_wrapper(sig.wrap_function(fn))

    def _make_decorator(self, func_name=None, *sigargs):
        def _decorator(fn):
            if func_name: fname = func_name
            else: fname = fn.__name__
            if sigargs: args=sigargs
            else: args= _get_annotations(fn)
            s = TypeCastSignature(*args)
            self._add_function(fname, s, fn)
            return fn
        return _decorator

    def register(self, *args):
        """Register a function with the context builder.

    Args:

      *args: the last argument must be the function to be registered. If there
        is more than one argument then the earlier arguments define the data
        conversion signature. If there are no earlier arguments then the
        signature is extracted from the function annotations.

        """

        # Called as a decorator with no signature arguments so decorator needs
        # to use function annotations
        if len(args) == 0: return self._make_decorator()

        # Called as a decorator with signature arguments
        if TypeCastSignature.is_return_element(args[-1]):
            return self._make_decorator(None, *args)

        # Called as a decorator or normal function with no signature arguments
        if len(args) == 1:
            return self._make_decorator(None)(args[0])

        # Called as a normal function with signature arguments
        sigargs=args[:-1]
        return self._make_decorator(None,*sigargs)(args[-1])

    def register_name(self, func_name, *args):
        """Register a function with assigning it a new name witin the context.

    Args:

      func_name: the new name for the function within the context.

      *args: the last argument must be the function to be registered. If there
        is more than one argument then the earlier arguments define the data
        conversion signature. If there are no earlier arguments then the
        signature is extracted from the function annotations.
        """

        if not func_name: raise ValueError("Specified an empty function name")

        # Called as a decorator with no signature arguments so decorator needs
        # to use function annotations
        if len(args) == 0: return self._make_decorator(func_name)

        # Called as a decorator with signature arguments
        if TypeCastSignature.is_return_element(args[-1]):
            return self._make_decorator(func_name, *args)

        # Called as a normal function with no signature arguments so need to use
        # function annotations
        if len(args) == 1: return self._make_decorator(func_name)(args[0])

        # Called as a normal function with signature arguments
        sigargs=args[:-1]
        return self._make_decorator(func_name,*sigargs)(args[-1])

    def make_context(self, cls_name="Context"):
        """Return a context object that encapsulates the registered functions"""

        tmp = { n : fn for n,fn in self._funcs.items() }
        return type(cls_name, (object,), tmp)()

#------------------------------------------------------------------------------
# main
#------------------------------------------------------------------------------
if __name__ == "__main__":
    raise RuntimeError('Cannot run modules')
