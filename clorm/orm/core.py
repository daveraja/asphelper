# -----------------------------------------------------------------------------
# Implementation of the core part of the Clorm ORM. In particular this provides
# the base classes and metaclasses for the definition of fields, predicates,
# predicate paths, and the specification of query conditions. Note: query
# condition specification is provided here because the predicate path comparison
# operators are overloads to return these objects. However, the rest of the
# query API is specified with the FactBase and select querying mechanisms
# (see factbase.py).
# ------------------------------------------------------------------------------

#import logging
#import os
import io
import contextlib
import inspect
import operator
import collections
import collections.abc as cabc
import bisect
import enum
import functools
import itertools
import clingo
import typing
import re
import uuid

__all__ = [
    'RawField',
    'IntegerField',
    'StringField',
    'ConstantField',
    'SimpleField',
    'Predicate',
    'ComplexTerm',
    'refine_field',
    'combine_fields',
    'define_nested_list_field',
    'simple_predicate',
    'path',
    'hashable_path',
    'alias',
    'not_',
    'and_',
    'or_',
    'joinall_'
    ]

#------------------------------------------------------------------------------
# Global
#------------------------------------------------------------------------------

# A compiled regular expression for matching an ASP constant term
g_constant_term_regex = re.compile("^_*[a-z][A-Za-z0-9_']*$")

#------------------------------------------------------------------------------
# A _classproperty decorator. (see
# https://stackoverflow.com/questions/3203286/how-to-create-a-read-only-class-property-in-python)
#------------------------------------------------------------------------------
class _classproperty(object):
    def __init__(self, getter):
        self.getter= getter
    def __get__(self, instance, owner):
        return self.getter(owner)

#------------------------------------------------------------------------------
# A descriptor for late initialisation of a read-only value. Helpful for delayed
# initialisation in metaclasses where an object needs to be created in the
# metaclass' __new__() call but can only be assigned in the __init__() call
# because the object needs to refer to the class being created in the
# metaclass. The assign() function can be called only once.
# ------------------------------------------------------------------------------
class _lateinit(object):
    def __init__(self,name):
        self._name = name
        self._value=None

    def assign(self, value):
        if self._value is not None:
            raise RuntimeError(("Error trying to reset the value for write-once "
                                "property {}").format(self._name))
        self._value=value

    def __get__(self, instance, owner):
        return self._value

#------------------------------------------------------------------------------
# Define the conditional ('where' clause) elements of a query.
#
# Note: the reason that this class are defined here rather than with the other
# aspects of the query API is because the PredicatePath class overloads the
# comparison operators to return a condition instance.
# ------------------------------------------------------------------------------

#------------------------------------------------------------------------------
# Conditional elements of a query (both the "where" clause as well as the "join"
# clause. A QCondition is either a boolean condition or a comparison condition
# depending on the operator. A comparison condition involves comparing a
# component of a fact (specified with a PredicatePath) against some criteria. A
# boolean condition specifies complex boolean relations consisting of comparion
# conditions and other boolean conditions.
# ------------------------------------------------------------------------------

# comparator functions that always return true (or false). This is useful for
# the cross product join operator that always returns true
def trueall(x,y): return True
def falseall(x,y): return True

# support functions to _wrap_query_condition in parentheses and wrap string
# comparison elements in quotes
def _wsce(a):
    if isinstance(a,str): return "'{}'".format(a)
    return "{}".format(a)
def _wqc(a):
    try:
        form = a.form
        if form == QCondition.Form.INFIX: return "({})".format(a)
        else: return "{}".format(a)
    except:
        return str(_wsce(a))


class QCondition(object):
    class Form(enum.Enum):
        UNIT=0
        INFIX=1
        FUNCTIONAL=2

    OpSig = collections.namedtuple('OpSig','arity form tostr')
    operators = {
        # Boolean operators
        operator.and_ : OpSig(2, Form.INFIX,
                              lambda x,y: "{} & {}".format(_wqc(x),_wqc(y))),
        operator.or_ : OpSig(2, Form.INFIX,
                             lambda x,y: "{} | {}".format(_wqc(x),_wqc(y))),
        operator.not_ : OpSig(1, Form.UNIT,
                              lambda x: "~{}".format(_wqc(x))),

        # Basic comparison operators
        operator.eq : OpSig(2, Form.INFIX,
                            lambda x,y: "{} == {}".format(_wsce(x),_wsce(y))),
        operator.ne : OpSig(2, Form.INFIX,
                            lambda x,y: "{} != {}".format(_wsce(x),_wsce(y))),
        operator.lt : OpSig(2, Form.INFIX,
                            lambda x,y: "{} < {}".format(_wsce(x),_wsce(y))),
        operator.le : OpSig(2, Form.INFIX,
                            lambda x,y: "{} <= {}".format(_wsce(x),_wsce(y))),
        operator.gt : OpSig(2, Form.INFIX,
                            lambda x,y: "{} > {}".format(_wsce(x),_wsce(y))),
        operator.ge : OpSig(2, Form.INFIX,
                            lambda x,y: "{} >= {}".format(_wsce(x),_wsce(y))),

        # A cross-product join operator
        trueall : OpSig(2, Form.FUNCTIONAL,
                        lambda x,y: "joinall_({},{})".format(path(x),path(y)))
    }

    def __init__(self, operator, *args):
        opsig = QCondition.operators.get(operator,None)
        if not opsig:
            raise ValueError("Unsupported operator {}".format(operator))
        if len(args) != opsig.arity:
            raise ValueError(("Operator {} expecting {} arguments but got "
                              "{}").format(operator, opsig.arity, len(args)))

        self._operator = operator
        self._args = tuple(args)

    @property
    def form(self):
        return QCondition.operators[self._operator].form

    @property
    def operator(self):
        return self._operator

    @property
    def args(self):
        return self._args

    def __and__(self,other):
        return QCondition(operator.and_,self,other)
    def __or__(self,other):
        return QCondition(operator.or_,self,other)
    def __rand__(self,other):
        return QCondition(operator.and_,self,other)
    def __ror__(self,other):
        return QCondition(operator.or_,self,other)
    def __invert__(self):
        return QCondition(operator.not_,self)

    def __eq__(self,other):
        def getval(val):
            if isinstance(val,PredicatePath): return val.meta.hashable
            return val

        if not isinstance(other, QCondition): return NotImplemented
        if self.operator != other.operator: return False
        for a,b in zip(self.args,other.args):
            if getval(a) != getval(b): return False
        return True

    def __ne__(self,other):
        result = self.__eq__(other)
        if result is NotImplemented: return NotImplemented
        return not result

    def __str__(self):
        opsig = QCondition.operators[self._operator]
        return opsig.tostr(*self._args)
        args = [ "({})".format(a) if isinstance(a,QCondition) else str(a) \
                 for a in self._args ]
        if opsig.arity == 1:
            return "{}{}".format(opsig.format,args[0])
        elif opsig.arity == 2:
            return "{} {} {}".format(args[0], opsig.format,args[1])
        else:
            return "{}({})".format(opsig.format,",".join([args]))

    def __repr__(self):
        return self.__str__()

# ------------------------------------------------------------------------------
# User callable functions to build QConditions
# ------------------------------------------------------------------------------

def not_(*conditions):
    '''Return a boolean condition that is the negation of the input condition'''
    return QCondition(operator.not_,*conditions)

def and_(*conditions):
    '''Return the conjunction of two of more conditions'''
    return functools.reduce((lambda x,y: QCondition(operator.and_,x,y)),conditions)

def or_(*conditions):
    '''Return the disjunction of two of more conditions'''
    return functools.reduce((lambda x,y: QCondition(operator.or_,x,y)),conditions)

def joinall_(*args):
    '''Return a cross-product join condition'''
    newargs = [ path(a) for a in args ]
    return QCondition(trueall, *newargs)

#------------------------------------------------------------------------------
# PredicatePath class and supporting metaclass and functions. The PredicatePath
# is crucial to the Clorm API because it implements the intuitive syntax for
# referring to elements of a fact; the sign as well as the fields and sub-fields
# (eg., Pred.sign, Pred.a.b or Pred.a[0]).
#
# When the API user refers to a field (or sign) of a Predicate sub-class they
# are redirected to the corresponding PredicatePath object of that predicate
# sub-class.
#
# Overview of how it works:
#
# Every Predicate sub-class has an attribute for every field of a predicate as
# well as providing a index lookup by position. Every non-tuple Predicate also
# has a sign attribute to say whether the fact/term is positive or negative.
#
# So, for each Predicate sub-class a corresponding PredicatePath sub-class is
# created that contains all these elements; defined as attributes and indexed
# items. An instance of this PredicatePath is created for each Predicate; which
# forms the root of a tree linking to to other PredicatePath (base or
# sub-classes) that represent the sub-paths. The leaves of the tree are base
# PredicatePath class objects while the non-leaf elements are sub-classes of
# PredicatePath.
#
# The result of building the PredicatePath tree is that each element of the tree
# encodes the path from the root node to that element. This is then used as a
# mechanism for forming queries and extracting components from facts.
#
# PredicatePath overloads the boolean comparison operators to return a
# functor. This provides the mechanism to construct "where" clauses that form
# part of a query. For example, the statement "P.a.b == 2" is overloaded to
# return a functor that takes any P instance p and checks whether p.a.b == 2.
#
# ------------------------------------------------------------------------------

def _define_predicate_path_subclass(predicate_class):
    class_name = predicate_class.__name__ + "_PredicatePath"
    return type(class_name, (PredicatePath,), { "_predicate_class" : predicate_class })

class _PredicatePathMeta(type):
    def __new__(meta, name, bases, dct):

        # For the base class don't do anything else
        if name == "PredicatePath":
            return super(_PredicatePathMeta, meta).__new__(meta, name, bases, dct)

        # Note: _predicate_class must be defined when creating a subclass.
        predicate_class = dct["_predicate_class"]
        if not predicate_class:
            raise AttributeError(("The \"_predicate_class\" member variable was not "
                                  "specified for {}").format(name))

        # Maintain a lookup of the fields that are complex.
        ct_classes = {}
        dct["_complexterm_classes"] = ct_classes

        def _make_lookup_functor(key):
            return lambda self: self._subpath[key]

        # Create an attribute for each predicate class field that returns an instance
        # of a pathbuilder for each attribute
        for fa in predicate_class.meta:
            dct[fa.name] = property(_make_lookup_functor(fa.name))
            ct_class = fa.defn.complex
            if ct_class: ct_classes[fa.name] = ct_class

        # If the corresponding Predicate is not a tuple then we need to create a
        # "sign" attribute.
        if not predicate_class.meta.is_tuple:
            dct["sign"] = property(_make_lookup_functor("sign"))

        # The appropriate fields have been created
        return super(_PredicatePathMeta, meta).__new__(meta, name, bases, dct)


class PredicatePath(object, metaclass=_PredicatePathMeta):
    '''PredicatePath implements the intuitive query syntax.

    PredicatePath provides a specification for refer to elements of a fact; both
    the sign as well as the fields and sub-fields of that fact (eg., Pred.sign,
    Pred.a.b or Pred.a[0]).

    When the API user refers to a field (or sign) of a Predicate sub-class they
    are redirected to the corresponding PredicatePath object of that predicate
    sub-class.

    While instances of this class (and sub-classes) are externally exposed
    through the API, users should not explicitly instantiate instances
    themselves.

    PredicatePath subclasses provide attributes and indexed items for refering
    to sub-paths. When a user specifies 'Pred.a.b.c' the Predicate class 'Pred'
    seemslessly passes off to an associated PredicatePath object, which then
    returns a path corresponding to the specifications.

    Fields can be specified either by name through a chain of attributes or
    using the overloaded __getitem__ array function which allows for name or
    positional argument specifications.

    The other important aspect of the PredicatePath is that it overloads the
    boolean operators to return a comparison condition. This is what allows for
    query specifications such as 'Pred.a.b == 2' or 'Pred.a.b == ph1_'

    Because the name 'meta' is a Clorm keyword and can't be used as a field name
    it is used as a property referring to an internal class with functions for
    use by the internals of the library. API users should not use this property.

    '''

    #--------------------------------------------------------------------------
    # An inner class that provides a hashable variant of a path. Because
    # PredicatePath co-ops the boolean comparision operators to return a
    # functor, rather than doing the normal behaviour of comparing two objects,
    # therefore it cannot be hashable (which required __eq__() __ne__() to work
    # properly). But we want to be able to use paths in a set or as a dictionary
    # key. So we provide a separate class to do this. The `path` property will
    # return the original (non-hashable) path.
    # --------------------------------------------------------------------------
    class Hashable(object):
        def __init__(self, path):
            self._path = path
            ps = self._path._pathseq
            base = (ps[0].predicate.__name__,ps[0].name)
            self._ordered = (base, ps[:1])

        @property
        def path(self):
            return self._path

        def __hash__(self):
            return hash(self._path._pathseq)

        def __eq__(self, other):
            if not isinstance(other, self.__class__): return NotImplemented
            return self._path._pathseq == other._path._pathseq

        def __ne__(self, other):
            result = self.__eq__(other)
            if result is NotImplemented: return NotImplemented
            return not result

        def __lt__(self,other):
            if not isinstance(other, self.__class__): return NotImplemented
            return self._ordered < other._ordered

        def __le__(self,other):
            result = self.__gt__(other)
            if result is NotImplemented: return NotImplemented
            return not result

        def __gt__(self,other):
            if not isinstance(other, self.__class__): return NotImplemented
            return self._ordered > other._ordered

        def __ge__(self,other):
            result = self.__lt__(other)
            if result is NotImplemented: return NotImplemented
            return not result

        def __str__(self):
            return str(self._path)

        def __repr__(self):
            return self.__str__()

    #--------------------------------------------------------------------------
    # An inner class to provide some useful functions in a sub-namespace. Need
    # this to avoid creating name conflicts, since each sub-class will have
    # attributes that mirror the field names of the associated
    # Predicate/Complex-term.  Internal API use only.
    # --------------------------------------------------------------------------

    class Meta(object):
        def __init__(self, parent):
            self._parent = parent
        #--------------------------------------------------------------------------
        # Properties of the parent PredicatePath instance
        # --------------------------------------------------------------------------
        @property
        def hashable(self):
            return self._parent._hashable

        # --------------------------------------------------------------------------
        # Is this a leaf path
        # --------------------------------------------------------------------------
        @property
        def is_leaf(self):
            return not hasattr(self, '_predicate_class')

        # --------------------------------------------------------------------------
        # attrgetter
        # --------------------------------------------------------------------------
        @property
        def attrgetter(self):
            return self._parent._attrgetter

        # --------------------------------------------------------------------------
        # Is this a root path (ie. the path corresponds to a predicate definition)
        # --------------------------------------------------------------------------
        @property
        def is_root(self):
            return len(self._parent._pathseq) == 1

        # --------------------------------------------------------------------------
        # Is this a path corresponding to a "sign" attribute
        # --------------------------------------------------------------------------
        @property
        def is_sign(self):
            return self._parent._pathseq[-1] == "sign"

        # --------------------------------------------------------------------------
        # Return the root path (ie. the path corresponds to a predicate definition)
        # --------------------------------------------------------------------------
        @property
        def root(self):
            if len(self._parent._pathseq) == 1: return self._parent
            pi = self._parent._pathseq[0]
            if pi.predicate.__name__ == pi.name: return self.predicate.meta.path
            return pi.predicate.meta.path_class([pi])

        # --------------------------------------------------------------------------
        # Return the Predicate sub-class that is the root of this path
        # --------------------------------------------------------------------------
        @property
        def predicate(self):
            return self._parent._pathseq[0].predicate

        # --------------------------------------------------------------------------
        # Return a dealiased version of this path
        # --------------------------------------------------------------------------
        @property
        def dealiased(self):
            pi = self._parent._pathseq[0]
            if pi.predicate.__name__ == pi.name: return self._parent
            dealised = pi.predicate.meta.path
            for key in self._parent._pathseq[1:]:
                dealised = dealised[key]
            return dealised


        #--------------------------------------------------------------------------
        # get the RawField instance associated with this path. If the path is a
        # root path or a sign path then it won't have an associated field so
        # will return None
        # --------------------------------------------------------------------------
        @property
        def field(self):
            return self._parent._field

        # --------------------------------------------------------------------------
        # All the subpaths of this path
        #--------------------------------------------------------------------------
        @property
        def subpaths(self):
            return self._parent._allsubpaths

        #--------------------------------------------------------------------------
        # Functions that do something with the parent PredicatePath instance
        #--------------------------------------------------------------------------

        #--------------------------------------------------------------------------
        # Resolve (extract the component) the path wrt a fact
        # --------------------------------------------------------------------------
        def resolve(self, fact):
            pseq = self._parent._pathseq
            if type(fact) != pseq[0].predicate:
                raise TypeError("{} is not of type {}".format(fact, pseq[0]))
            return self._parent._attrgetter(fact)

    #--------------------------------------------------------------------------
    # Return the underlying meta object with useful functions
    # Internal API use only
    #--------------------------------------------------------------------------
    @property
    def meta(self):
        return self._meta

    #--------------------------------------------------------------------------
    # Takes a pathseq - which is a sequence where the first element must be a
    # Predicate class and subsequent elements are strings refering to
    # attributes.
    #--------------------------------------------------------------------------
    def __init__(self, pathseq):
        self._meta = PredicatePath.Meta(self)
        self._pathseq = tuple(pathseq)
        self._subpath = {}
        self._allsubpaths = tuple([])
        self._field = self._get_field()
        self._hashable = PredicatePath.Hashable(self)
        tmp = pathseq[1:]
        if not tmp: self._attrgetter = lambda x: x
        else: self._attrgetter = operator.attrgetter(".".join(tmp))


        if not pathseq or not isinstance(pathseq[0], PathIdentity) or \
           not inspect.isclass(pathseq[0].predicate) or \
           not issubclass(pathseq[0].predicate, Predicate):
            raise TypeError(("Internal error: invalid base path sequence for "
                             "predicate path definition: {}").format(pathseq))

        # If this is a leaf path (instance of the base PredicatePath class) then
        # there will be no sub-paths so nothing else to do.
        if not hasattr(self, '_predicate_class'): return

        # Iteratively build the tree of PredicatePaths corresponding to the
        # searchable elements. Elements corresponding to non-complex terms will
        # have leaf PredicatePaths while the complex ones will have appropriate
        # sub-classed PredicatePaths.
        for fa in self._predicate_class.meta:
            name = fa.name
            idx = fa.index
            if name in self._complexterm_classes:
                path_cls = self._complexterm_classes[name].meta.path_class
            else:
                path_cls = PredicatePath
            path = path_cls(list(self._pathseq) + [name])
            self._subpath[name] = path
            self._subpath[idx] = path

        # Add the sign if it's not a tuple
        if not self._predicate_class.meta.is_tuple:
            self._subpath["sign"] = PredicatePath(list(self._pathseq) + ["sign"])

        # A list of the unique subpaths
        self._allsubpaths = tuple([sp for key,sp in self._subpath.items() \
                                   if not isinstance(key,int)])

    #--------------------------------------------------------------------------
    # Helper function to compute the field of the path (or None if not exists)
    # --------------------------------------------------------------------------
    def _get_field(self):
        if len(self._pathseq) <= 1: return None
        if self._pathseq[-1] == "sign": return None
        predicate = self._pathseq[0].predicate
        for name in self._pathseq[1:]:
            field = predicate.meta[name].defn
            if field.complex: predicate = field.complex
        return field

    #--------------------------------------------------------------------------
    # A PredicatePath instance is a functor that resolves a fact wrt the path
    # --------------------------------------------------------------------------
    def __call__(self, fact):
        pseq = self._pathseq
        if type(fact) != pseq[0].predicate:
            raise TypeError("{} is not of type {}".format(fact, pseq[0]))
        return self._attrgetter(fact)

    #--------------------------------------------------------------------------
    # Get all field path builder corresponding to an index
    # --------------------------------------------------------------------------
    def __getitem__(self, key):
        try:
            return self._subpath[key]
        except:
            if self.meta.is_leaf:
                raise KeyError("Leaf path {} has no sub-paths".format(self))
            msg = "{} is not a valid positional argument for {}"
            raise KeyError(msg.format(key, self._predicate_class))

    #--------------------------------------------------------------------------
    # Overload the boolean operators to return a functor
    #--------------------------------------------------------------------------
    def __eq__(self, other):
        return QCondition(operator.eq, self, other)
    def __ne__(self, other):
        return QCondition(operator.ne, self, other)
    def __lt__(self, other):
        return QCondition(operator.lt, self, other)
    def __le__(self, other):
        return QCondition(operator.le, self, other)
    def __gt__(self, other):
        return QCondition(operator.gt, self, other)
    def __ge__(self, other):
        return QCondition(operator.ge, self, other)

    #--------------------------------------------------------------------------
    # String representation
    # --------------------------------------------------------------------------

    def __str__(self):
        def basename(): return self._pathseq[0].name
        if len(self._pathseq) == 1: return basename()

        tmp = ".".join(self._pathseq[1:])
        return basename() + "." + tmp

    def __repr__(self):
        return self.__str__()

#------------------------------------------------------------------------------
# API function to return the PredicatePath for the predicate class itself. This
# is the best way to support syntax such as "Pred == ph1_" in a query without
# trying to do strange overloading of the class comparison operator.
# ------------------------------------------------------------------------------

def path(arg,exception=True):
    '''Return the predicate path (which is a path to some fact component)

    Will try to resolve the input cleverly so if input a predicate path will
    simply return that path, if input a predicate subclass will return a path
    corresponding to that path or if input a hashable path will return the
    corresponding path object.

    '''
    if isinstance(arg, PredicatePath): return arg
    elif isinstance(arg, PredicatePath.Hashable): return arg.path
    elif inspect.isclass(arg) and issubclass(arg, Predicate): return arg.meta.path
    if not exception: return None
    raise TypeError(("Invalid argument {} (type: {}): expecting either a "
                     "PredicatePath, a Predicate sub-class, or a "
                     "PredicatePath.Hashable").format(arg, type(arg)))

#------------------------------------------------------------------------------
# API function to return the PredicatePath.Hashable instance for a path
# ------------------------------------------------------------------------------

def hashable_path(arg,exception=True):
    '''Return a PredicatePath.Hashable instance for a path or Predicate sub-class.

    A hashable path can be used in a set or dictionary key. If the argument is a
    path then returns the hashable version (the original path can be accessed
    from the hashable's "path" property). If the argument is a Predicate
    sub-class then returns the hashable path corresponding to the root path for
    that predicate class.

    '''
    if isinstance(arg,PredicatePath.Hashable):
        return arg
    elif isinstance(arg, PredicatePath):
        return arg.meta.hashable
    elif inspect.isclass(arg) and issubclass(arg, Predicate):
        return arg.meta.path.meta.hashable
    if not exception: return None
    raise TypeError(("Invalid argument {} (type: {}): expecting either a "
                     "Predicate sub-class or a PredicatePath or a "
                     "PredicatePath.Hashable").format(arg, type(arg)))


#------------------------------------------------------------------------------
# API function to return the an alias path for a predicate
# ------------------------------------------------------------------------------

def alias(predicate, name=None):
    '''Return an alias PredicatePath instance for a Predicate sub-class.

    A predicate alias can be used to support joins in queries. The alias has all
    the same fields (and sub-fields) as the "normal" path associated with the
    predicate.

    Example:
       .. code-block:: python

           import clorm Predicate, ConstantField

           class F(Predicate):
               a = ConstantField
               b = ConstantField

          X = alias(F)
          print("field of predicate F: {}".format(F.a))
          print("field of alias X: {}".format(X.a))

    '''
    if inspect.isclass(predicate) and issubclass(predicate, Predicate):
        return predicate.meta.alias(name)

    errormsg = ("predicate argument must refer to a Predicate sub-class "
                "or the root path corresponding to a Predicate sub-class")
    arg = predicate
    if isinstance(arg,PredicatePath.Hashable): arg = predicate.path
    if isinstance(arg, PredicatePath):
        if not arg.meta.is_root:
            raise ValueError("Invalid argument {}: {}".format(arg,errormsg))
        return arg.meta.predicate.meta.alias(name)

    raise ValueError("Invalid argument {}: {}".format(arg,errormsg))

#------------------------------------------------------------------------------
# API function to return a de-aliased path. If the path is not an alias returns
# itself.
# ------------------------------------------------------------------------------

def dealiased_path(path):
    if inspect.isclass(arg) and issubclass(arg, Predicate): return arg.meta.path

    def getpath():
        if isinstance(arg, PredicatePath): return arg
        elif isinstance(arg, PredicatePath.Hashable): return arg.path
        if not exception: return None
        raise TypeError(("Invalid argument {} (type: {}): expecting either a "
                         "PredicatePath, a Predicate sub-class, or a "
                         "PredicatePath.Hashable").format(arg, type(arg)))
    cleanpath = getpath()
    if cleanpath is None: return None
    return cleanpath.meta.dealised


#------------------------------------------------------------------------------
# Helper function to check if a second set of keys is a subset of a first
# set. If it is not it returns the unrecognised keys. Useful for checking a
# function that uses **kwargs.
# ------------------------------------------------------------------------------

def kwargs_check_keys(validkeys, inputkeys):
    if not inputkeys.issubset(validkeys): return inputkeys-validkeys
    return set([])



#------------------------------------------------------------------------------
# RawField class captures the definition of a logical term ("which we will call
# a field") between python and clingo.
# ------------------------------------------------------------------------------
def _make_pytocl(fn):
    def _pytocl(cls, v):
        if cls._parentclass:
            return cls._parentclass.pytocl(fn(v))
        return fn(v)
    return _pytocl

def _make_cltopy(fn):
    def _cltopy(cls, v):
        if cls._parentclass:
            return fn(cls._parentclass.cltopy(v))
        return fn(v)
    return _cltopy

def _rfm_constructor(self, *args, **kwargs):
    # Check the match between positional and keyword arguments
    if "default" in kwargs and len(args) > 0:
        raise TypeError(("Field constructor got multiple values for "
                         "argument 'default'"))
    if "index" in kwargs and len(args) > 1:
        raise TypeError(("Field constructor got multiple values for "
                         "argument 'index'"))
    if len(args) > 2:
        raise TypeError(("Field constructor takes from 0 to 2 positional"
                         "arguments but {} given").format(len(args)))

    # Check for bad positional arguments
    badkeys = kwargs_check_keys(set(["default","index"]), set(kwargs.keys()))
    if badkeys:
        mstr = "Field constructor got unexpected keyword arguments: "
        if len(badkeys) == 1:
            mstr = "Field constructor got an unexpected keyword argument: "
        raise TypeError("{}{}".format(mstr,",".join(sorted(badkeys))))

    if "default" in kwargs: self._default = (True, kwargs["default"])
    elif len(args) > 0: self._default = (True, args[0])
    else: self._default = (False,None)

    if "index" in kwargs: self._index = kwargs["index"]
    elif len(args) > 1: self._index = args[1]
    else: self._index=False

    if not self._default[0]: return
    dval = self._default[1]

    # Check that the default is a valid value. If the default is a callable then
    # we can't do this check because it could break a counter type procedure.
    if not callable(dval):
        try:
            self.pytocl(dval)
        except (TypeError,ValueError):
            raise TypeError("Invalid default value \"{}\" for {}".format(
                dval, type(self).__name__))

class _RawFieldMeta(type):
    def __new__(meta, name, bases, dct):

        # Add a default initialiser if one is not already defined
        if "__init__" not in dct:
            dct["__init__"] = _rfm_constructor

        dct["_fpb"] = _lateinit("{}._fpb".format(name))

        if name == "RawField":
            dct["_parentclass"] = None
            return super(_RawFieldMeta, meta).__new__(meta, name, bases, dct)

        for key in [ "cltopy", "pytocl" ]:
            if key in dct and not callable(dct[key]):
                raise AttributeError("Definition of {} is not callable".format(key))

        parents = [ b for b in bases if issubclass(b, RawField)]
        if len(parents) == 0:
            raise TypeError("Internal bug: number of RawField bases is 0!")
        if len(parents) > 1:
            raise TypeError("Multiple class inheritence for field classes is forbidden")
        dct["_parentclass"] = parents[0]

        # When a conversion is not specified raise a NotImplementedError
        def _raise_cltopy_nie(cls,v):
            msg=("'{}' is only partially specified and has no "
                 "Clingo to Python (cltopy) conversion").format(name)
            raise NotImplementedError(msg)
        def _raise_pytocl_nie(cls,v):
            msg=("'{}' is only partially specified and has no "
                 "Python to Clingo (cltopy) conversion").format(name)
            raise NotImplementedError(msg)

        if "cltopy" in dct:
            dct["cltopy"] = classmethod(_make_cltopy(dct["cltopy"]))
        else:
            dct["cltopy"] = classmethod(_raise_cltopy_nie)

        if "pytocl" in dct:
            dct["pytocl"] = classmethod(_make_pytocl(dct["pytocl"]))
        else:
            dct["pytocl"] = classmethod(_raise_pytocl_nie)


        # For complex-terms provide an interface to the underlying complex term
        # object
        if "complex" in dct:
            dct["complex"] = _classproperty(dct["complex"])
        else:
            dct["complex"] = _classproperty(lambda cls: None)
#            dct["complex"] = _classproperty(None)

        return super(_RawFieldMeta, meta).__new__(meta, name, bases, dct)

    def __init__(cls, name, bases, dct):

        return super(_RawFieldMeta, cls).__init__(name, bases, dct)

#------------------------------------------------------------------------------
# Field definitions. All fields have the functions: pytocl, cltopy,
# and unifies, and the properties: default and has_default
# ------------------------------------------------------------------------------

class RawField(object, metaclass=_RawFieldMeta):
    """A class that represents a field that correspond to logical terms.

    A field is typically used as part of a ``ComplexTerm`` or ``Predicate``
    definition. It defines the data type of an ASP term and provides functions
    for translating the term to a more convenient Python type.

    It contains two class functions ``cltopy`` and ``pytocl`` that implement the
    translation from Clingo to Python and Python to Clingo respectively. For
    ``RawField`` these functions simply pass the values straight though, however
    ``RawField`` can be sub-classed to build a chain of
    translations. ``StringField``, ``IntegerField``, and ``ConstantField`` are
    predefined sub-classes that provide translations for the ASP simple terms;
    *string*, *integer* and *constant*.

    To sub-class RawField (or one of its sub-classes) simply specify ``cltopy``
    and ``pytocl`` functions that take an input and perform some translation to
    an output format.

    Note: the ``cltopy`` and ``pytocl`` functions are legitmately allowed to
    throw either a ``TypeError`` or ``ValueError`` exception when provided with
    bad input. These exceptions will be treated as a failure to unify when
    trying to unify clingo symbols to facts. However, any other exception is
    passed through as a genuine error.  This should be kept in mind if you are
    writing your own field class.

    Example:
       .. code-block:: python

           import datetime

           class DateField(StringField):
                     pytocl = lambda dt: dt.strftime("%Y%m%d")
                     cltopy = lambda s: datetime.datetime.strptime(s,"%Y%m%d").date()


       Because ``DateField`` sub-classes ``StringField``, rather than
       sub-classing ``RawField`` directly, it forms a longer data translation
       chain:

         clingo symbol object -- RawField -- StringField -- DateField -- python date object

       Here the ``DateField.cltopy`` is called at the end of the chain of
       translations, so it expects a Python string object as input and outputs a
       date object. ``DateField.pytocl`` does the opposite and inputs a date
       object and is expected to output a Python string object.

    Args:

      default: A default value (or function) to be used when instantiating a
       ``Predicate`` or ``ComplexTerm`` object. If a Python ``callable`` object is
       specified (i.e., a function or functor) then it will be called (with no
       arguments) when the predicate/complex-term object is instantiated.

      index (bool): Determine if this field should be indexed by default in a
        ``FactBase```. Defaults to ``False``.

    """

    @classmethod
    def cltopy(cls, v):
        """Called when translating data from Clingo to Python"""
        return v

    @classmethod
    def pytocl(cls, v):
        """Called when translating data from Python to Clingo"""
        return v

    @classmethod
    def unifies(cls, v):
        """Returns whether a `Clingo.Symbol` can be unified with this type of term"""
        try:
            cls.cltopy(v)
        except (TypeError,ValueError):
            return False
        return True

    # Internal property - not part of official API
    @_classproperty
    def complex(cls):
        return None

    @property
    def has_default(self):
        """Returns whether a default value has been set"""
        return self._default[0]

    @property
    def default(self):
        """Returns the default value for the field (or ``None`` if no default was set).

        Note: 1) if a function was specified as the default then testing
        ``default`` will call this function and return the value, 2) if your
        RawField sub-class allows a default value of ``None`` then you need to
        check the ``has_default`` property to distinguish between no default
        value and a ``None`` default value.

        """
        if not self._default[0]: return None
        if callable(self._default[1]): return self._default[1]()
        return self._default[1]

    @property
    def index(self):
        """Returns whether this field should be indexed by default in a `FactBase`"""
        return self._index

#------------------------------------------------------------------------------
# StringField and IntegerField are simple sub-classes of RawField
#------------------------------------------------------------------------------

class StringField(RawField):
    """A field to convert between a Clingo.String object and a Python string."""

    def cltopy(raw):
        if raw.type != clingo.SymbolType.String:
            raise TypeError("Object {0} is not a clingo.String symbol")
        return raw.string

    pytocl = lambda v: clingo.String(v)

class IntegerField(RawField):
    """A field to convert between a Clingo.Number object and a Python integer."""
    def cltopy(raw):
        if raw.type != clingo.SymbolType.Number:
            raise TypeError("Object {0} is not a clingo.Number symbol")
        return raw.number

    pytocl = lambda v: clingo.Number(v)

#------------------------------------------------------------------------------
# ConstantField is more complex than basic string or integer because the value
# can be negated. A heavy handed way to deal with this would be to create a
# unary ComplexTerm subclass for every constant string value. But this is an
# expensive way of dealing with the boundary case of negated constants that will
# be used rarely (I've never seen it used in the wild).
#
# Instead we encode this as a string with a minus first symbol. The disadvantage
# of this approach is that detecting complementary terms will need to be done
# manually. But I think this is a good trade-off since it is very unusual to use
# negated terms in general and negated constants in particular.
# ------------------------------------------------------------------------------

class ConstantField(RawField):
    """A field to convert between a simple ``Clingo.Function`` object and a Python
    string.

    Note: currently ``ConstantField`` treats a string with a starting "-" as a
    negated constant. In hindsight this was a mistake and is now
    *deprecated*. While I don't think anyone actually used this functionality
    (since it was never documented) nevertheless I will keep it there until the
    Clorm version 2.0 release.

    """
    def cltopy(raw):
        if   (raw.type != clingo.SymbolType.Function or
              not raw.name or len(raw.arguments) != 0):
            raise TypeError(("Clingo symbol object '{}' is not a unary Function "
                             "symbol").format(raw))
        return raw.name if raw.positive else "-{}".format(raw.name)

    def pytocl(v):
        if not isinstance(v,str):
            raise TypeError("Value '{}' is not a string".format(v))
        if v.startswith('-'): return clingo.Function(v[1:],[],False)
        return clingo.Function(v,[])


#------------------------------------------------------------------------------
# A SimpleField can handle any simple term (constant, string, integer).
#------------------------------------------------------------------------------

class SimpleField(RawField):
    """A class that represents a field corresponding to any simple term: *string*,
    *constant*, or *integer*.

    Converting from an ASP string, constant, or integer will produce the
    expected Python string or integer object. However, since ASP strings and
    constants both map to Python strings therefore converting from Python to ASP
    is less straightforward. In this case it uses a regular expression to
    determine if the string matches an ASP constant or if it should be treated
    as a quoted string.

    Because of this potential for ambiguity it is often better to use the
    distinct ``IntegerField``, ``ConstantField``, and ``StringField`` classes
    rather than the ``SimpleField`` class.

    """
    def cltopy(raw):
        if raw.type == clingo.SymbolType.String:
            return raw.string
        elif raw.type == clingo.SymbolType.Number:
            return raw.number
        elif raw.type == clingo.SymbolType.Function:
            if len(raw.arguments) == 0 and raw.positive:
                return raw.name
        raise TypeError("Not a simple term (string/constant/integer)")

    def pytocl(value):
        if isinstance(value,int):
            return clingo.Number(value)
        elif not isinstance(value,str):
            raise TypeError("No translation to a simple term")
        if g_constant_term_regex.match(value):
            return clingo.Function(value,[])
        else:
            return clingo.String(value)

#------------------------------------------------------------------------------
# refine_field is a function that creates a sub-class of a RawField (or RawField
# sub-class). It restricts the set of allowable values based on a functor or an
# explicit set of values.
# ------------------------------------------------------------------------------
#------------------------------------------------------------------------------
# Helper function to define a sub-class of a RawField (or sub-class) that
# restricts the allowable values.
# ------------------------------------------------------------------------------

# Support for refine_field
def _refine_field_functor(subclass_name, field_class, valfunc):
    def _test_value(v):
        if not valfunc(v):
            raise TypeError(("Invalid value \"{}\" for {} (restriction of "
                             "{})").format(v, subclass_name, field_class.__name__))
        return v

    return type(subclass_name, (field_class,),
                { "pytocl": _test_value,
                  "cltopy": _test_value})

# Support for refine_field
def _refine_field_collection(subclass_name, field_class, values):
    # Check that the values are all valid
    for v in values:
        try:
            out = field_class.pytocl(v)
        except (TypeError,ValueError):
            raise TypeError("Invalid value \"{}\" for {}".format(
                v, field_class.__name__))

    # Now define the restricted pytocl and cltopy functions
    fs = frozenset(values)
    def _test_value(v):
        if v not in fs:
            raise TypeError(("Invalid value \"{}\" for {} (restriction of "
                             "{})").format(v, subclass_name, field_class.__name__))
        return v

    return type(subclass_name, (field_class,),
                { "pytocl": _test_value,
                  "cltopy": _test_value})

def refine_field(*args):
    """Factory function that returns a field sub-class with restricted values.

    A helper factory function to define a sub-class of a RawField (or sub-class)
    that restricts the allowable values. For example, if you have a constant in
    a predicate that is restricted to the days of the week ("monday", ...,
    "sunday"), you then want the Python code to respect that restriction and
    throw an error if the user enters the wrong value (e.g. a spelling error
    such as "wednsday"). Restrictions are also useful for unification if you
    want to unify based on some specific value.

    Example:
       .. code-block:: python

           WorkDayField = refine_field("WorkDayField", ConstantField,
              ["monday", "tuesday", "wednesday", "thursday", "friday"])

          class WorksOn(Predicate):
              employee = ConstantField()
              workday = WorkdDayField()

    Instead of a passing a list of values the last parameter can also be a
    function/functor. If the last parameter is callable then it is treated as a
    function that takes a field value and returns true if it is a valid value.

    Example:
       .. code-block:: python

           PosIntField = refine_field("PosIntField", NumberField,
              lambda x : x >= 0)

    The function must be called using positional arguments with either 2 or 3
    arguments. For the 3 argument case a class name is specified for the name of
    the new field. For the 2 argument case an anonymous field class name is
    automatically generated.

    Example:
       .. code-block:: python

           WorkDayField = refine_field(ConstantField,
              ["monday", "tuesday", "wednesday", "thursday", "friday"])

    Only positional arguments are supported.

    Args:

       subclass_name (optional): new sub-class name (anonymous if none specified).

       field_class: the field that is being sub-classed

       values|functor: a list of values or a functor to determine validity

    """
    largs = len(args)
    if largs == 2:
        field_class = args[0]
        values = args[1]
        subclass_name = field_class.__name__ + "_Restriction"
    elif largs == 3:
        subclass_name = args[0]
        field_class = args[1]
        values = args[2]
    else:
        raise TypeError("refine_field() missing required positional arguments")

    if not inspect.isclass(field_class) or not issubclass(field_class,RawField):
        raise TypeError("{} is not a subclass of RawField".format(field_class))

    if callable(values):
        return _refine_field_functor(subclass_name, field_class, values)
    else:
        return _refine_field_collection(subclass_name, field_class, values)



#------------------------------------------------------------------------------
# combine_fields is a function that creates a sub-class of RawField that
# combines existing RawField subclasses. It is the mirror of the refine_field
# helper function.
# ------------------------------------------------------------------------------

def combine_fields(*args):
    """Factory function that returns a field sub-class that combines other fields

    A helper factory function to define a sub-class of RawField that combines
    other RawField subclasses. The subclass is defined such that it's
    ``pytocl()`` (respectively ``cltopy()``) function tries to return the value
    returned by the underlying sub-field's ``pytocl()`` ( (respectively
    ``cltopy()``) function. If the first sub-field fails then the second is
    called, and so on until there are no matching sub-fields. If there is no
    match then a TypeError is raised.

    Example:
       .. code-block:: python

          MixedField = combine_fields("MixedField",[ConstantField,IntegerField])

    Only positional arguments are supported.

    Args:

       subclass_name (optional): new sub-class name (anonymous if none specified).

       field_subclasses: the fields to combine

    """

    # Deal with the optional subclass name
    largs=len(args)
    if largs == 1:
        subclass_name="AnonymousCombinedRawField"
        fields=args[0]
    elif largs == 2:
        subclass_name=args[0]
        fields=args[1]
    else:
        raise TypeError("combine_fields() missing or invalid arguments")

    # Must combine at least two fields otherwise it doesn't make sense
    for f in fields:
        if not inspect.isclass(f) or not issubclass(f,RawField):
            raise TypeError("{} is not RawField or a sub-class".format(f))
    if len(fields) < 2:
        raise TypeError("Must specify at least two fields to combine")

    fields=tuple(fields)
    def _pytocl(v):
        for f in fields:
            try:
                return f.pytocl(v)
            except (TypeError, ValueError):
                pass
        raise TypeError("No combined pytocl() match for value {}".format(v))

    def _cltopy(r):
        for f in fields:
            try:
                return f.cltopy(r)
            except (TypeError, ValueError):
                pass
        raise TypeError("No combined cltopy() match for clingo symbol {}".format(r))

    return type(subclass_name, (RawField,),
                { "pytocl": _pytocl,
                  "cltopy": _cltopy})

#------------------------------------------------------------------------------
# define_nested_list_field is a function that creates a sub-class of RawField that
# deals with nested list encoded asp.
# ------------------------------------------------------------------------------

def define_nested_list_field(*args):
    """Factory function that returns a RawField sub-class for nested lists

    ASP doesn't have an explicit notion of a list, but sometimes it is useful to
    encode a list as a series of nested pairs (ie., a head-tail encoding) with
    an empty tuple indicating the end of the list.

    Example:
       .. code-block:: prolog

          (1,(2,(3,())))         % Encodes a list [1,2,3]

    This function is a helper factory function to define a sub-class of RawField
    that deals with a nested list encoding consisting of elements of some other
    RawField subclass.

    Example:
       .. code-block:: python

          # Unifies against a nested list of constants
          NestedListField = define_nested_list_field("NLField",ConstantField)

    Only positional arguments are supported.

    Args:

       subclass_name (optional): new sub-class name (anonymous if none specified).

       element_definition: the field type for each list element

    """

    # Deal with the optional subclass name
    largs=len(args)
    if largs == 1:
        subclass_name="AnonymousNestedListField"
        efield=args[0]
    elif largs == 2:
        subclass_name=args[0]
        efield=args[1]
    else:
        raise TypeError("define_nested_list_field() missing or invalid arguments")

    # The element_field must be a RawField sub-class
    if not inspect.isclass(efield) or not issubclass(efield,RawField):
        raise TypeError("'{}' is not a RawField or a sub-class".format(efield))

    def _pytocl(v):
        if isinstance(v,str) or not isinstance(v,cabc.Iterable):
            raise TypeError("'{}' is not a collection (list/seq/etc)".format(v))
        nested=clingo.Function("",[])
        for ev in reversed(v):
            nested=clingo.Function("",[efield.pytocl(ev),nested])
        return nested

    def _get_next(raw):
        if raw.type != clingo.SymbolType.Function or raw.name != "":
            raise TypeError("'{}' is not a nested list".format(raw))
        rlen = len(raw.arguments)
        if rlen == 0: return None
        if rlen == 2: return raw.arguments
        else:
            raise TypeError("'{}' is not a nested list".format(raw))

    def _cltopy(raw):
        elements=[]
        result = _get_next(raw)
        while result:
            elements.append(efield.cltopy(result[0]))
            result = _get_next(result[1])
        return elements

    return type(subclass_name, (RawField,),
                { "pytocl": _pytocl,
                  "cltopy": _cltopy})

#------------------------------------------------------------------------------
# FieldAccessor - a Python descriptor (similar to a property) to access the
# value associated with a field. It has a __get__ overload to return the data of
# the field if the function is called from an instance, but if called by the
# class then returns the appropriate PredicatePath (that can be used to specify
# a query).
# ------------------------------------------------------------------------------
class FieldAccessor(object):
    def __init__(self, name, index, defn):
        self._name = name
        self._index = index
        self._defn = defn
        self._parent_cls = None

    @property
    def name(self): return self._name

    @property
    def index(self): return self._index

    @property
    def defn(self): return self._defn

    @property
    def parent(self): return self._parent_cls

    @parent.setter
    def parent(self, pc):
        if self._parent_cls:
            raise RuntimeError(("Trying to reset the parent for a "
                                "FieldAccessor doesn't make sense"))
        self._parent_cls = pc

    def __get__(self, instance, owner=None):
        if not instance:
            # Return the PredicatePath object corresponding to this field
            return self.parent.meta.path[self._index]

        if not isinstance(instance, self._parent_cls):
            raise TypeError(("field {} doesn't match type "
                             "{}").format(self, type(instance).__name__))
        return instance._field_values[self._index]

    def __set__(self, instance, value):
        raise AttributeError(("Cannot modify {}.{}: field values are "
                              "read-only").format(self.parent.__name__, self.name))

#------------------------------------------------------------------------------
# SignAccessor - a Python descriptor to access the sign value of a
# Predicate instance. It has a __get__ overload to return the value of
# the sign if the function is called from an instance, but if called by the
# class then returns the appropriate PredicatePath (that can be used to
# specify a query).
# ------------------------------------------------------------------------------

class SignAccessor(object):
    def __init__(self):
        self._parent_cls = None

    @property
    def parent(self): return self._parent_cls

    @parent.setter
    def parent(self, pc):
        if self._parent_cls:
            raise RuntimeError(("Trying to reset the parent for a "
                                "SignAccessor doesn't make sense"))
        self._parent_cls = pc

    def __get__(self, instance, owner=None):
        if not instance:
            # Return the PredicatePath object corresponding to this sign
            return self.parent.meta.path.sign

        if not isinstance(instance, self._parent_cls):
            raise TypeError(("sign {} doesn't match type "
                             "{}").format(self, type(instance).__name__))
        return instance._raw.positive

    def __set__(self, instance, value):
        raise AttributeError(("Cannot modify {}.sign: sign and field values "
                              "are read-only").format(self.parent.__name__))


#------------------------------------------------------------------------------
# Helper function to cleverly handle a field definition. If the input is an
# instance of a RawField then simply return the object. If it is a subclass of
# RawField then return an instantiation of the object. If it is a tuple then
# treat it as a recursive definition and return an instantiation of a
# dynamically created complex-term corresponding to a tuple (with the class name
# ClormAnonTuple).
# ------------------------------------------------------------------------------

def get_field_definition(defn):
    errmsg = ("Unrecognised field definition object '{}'. Expecting: "
              "1) RawField (sub-)class, 2) RawField (sub-)class instance, "
              "3) a tuple containing a field definition")

    # If we get a RawField (sub-)class then return an instance with default init
    if inspect.isclass(defn):
        if not issubclass(defn,RawField): raise TypeError(errmsg.format(defn))
        return defn()

    # Simplest case of a RawField instance
    if isinstance(defn,RawField): return defn

    # Expecting a tuple and treat it as a recursive definition
    if not isinstance(defn, tuple): raise TypeError(errmsg.format(defn))

    # NOTE: I was using a dict rather than OrderedDict which just happened to
    # work. Apparently, in Python 3.6 this was an implmentation detail and
    # Python 3.7 it is a language specification (see:
    # https://stackoverflow.com/questions/1867861/how-to-keep-keys-values-in-same-order-as-declared/39537308#39537308).
    # However, since Clorm is meant to be Python 3.5 compatible change this to
    # use an OrderedDict.
    # proto = { "arg{}".format(i+1) : get_field_definition(d) for i,d in enumerate(defn) }
    proto = collections.OrderedDict([("arg{}".format(i+1), get_field_definition(d))
                                     for i,d in enumerate(defn)])
    proto['Meta'] = type("Meta", (object,), {"is_tuple" : True, "_anon" : True})
    ct = type("ClormAnonTuple", (Predicate,), proto)
    return ct.Field()


#------------------------------------------------------------------------------
# Return the list of field_paths associated with a predicate (ignoring the base
# predicate path itself).
# ------------------------------------------------------------------------------
def _get_paths(predicate):
    def get_subpaths(path):
        paths=[]
        for subpath in path.meta.subpaths:
            paths.append(subpath)
            paths.extend(get_subpaths(subpath))
        return paths

    return get_subpaths(path(predicate))

#------------------------------------------------------------------------------
# Return the list of field_paths that are specified as indexed
#------------------------------------------------------------------------------
def _get_paths_for_default_indexed_fields(predicate):
    def is_indexed(path):
        field = path.meta.field
        if field and field.index: return True
        return False
    return filter(is_indexed, _get_paths(predicate))

# ------------------------------------------------------------------------------
# Determine if an attribute name has the pattern of an official attribute
# (ie.  has name of the form __XXX__).
# ------------------------------------------------------------------------------

def _magic_name(name):
    if not name.startswith("__"): return False
    if not name.endswith("__"): return False
    if len(name) <= 4: return False
    if name[2] == '_': return False
    if name[-3] == '_': return False
    return True

#------------------------------------------------------------------------------
# The Predicate base class and supporting functions and classes
#------------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# We want to support aliases to predicate paths so that we can define a join
# query between a predicate and itself. Eg. something like F.a == F.alias().a So
# we need a way to distinguish between paths that point to the same underlying
# predicate but use a different identifier.
# -----------------------------------------------------------------------------

PathIdentity = collections.namedtuple("PathIdentity", "predicate name")

#--------------------------------------------------------------------------
# One PredicateDefn object for each Predicate sub-class
#--------------------------------------------------------------------------
class PredicateDefn(object):

    """Encapsulates some meta-data for a Predicate definition.

    Each Predicate class will have a corresponding PredicateDefn object that specifies some
    introspective properties of the predicate/complex-term.

    """

    def __init__(self, name, field_accessors, anon=False,sign=None):
        self._name = name
        self._byidx = tuple(field_accessors)
        self._byname = { f.name : f for f in field_accessors }
        self._arity = len(self._byidx)
        self._anon = anon
        self._key2canon = { f.index : f.name for f in field_accessors }
        self._key2canon.update({f.name : f.name for f in field_accessors })
        self._parent_cls = None
        self._indexed_fields = ()
        self._sign = sign

    @property
    def name(self):
        """Returns the string name of the predicate or complex term"""
        return self._name

    @property
    def arity(self):
        """Returns the arity of the predicate"""
        return self._arity

    @property
    def sign(self):
        """Returns the sign that this Predicate signature can unify against

           If the sign is ``True`` then this Predicate definition will only
           unify against positive literals. If the sign is ``False`` then it
           will only unify against negative literals, and if ``None`` then it
           will unify against either positive or negative literals.

        """
        return self._sign

    @property
    def is_tuple(self):
        """Returns true if the definition corresponds to a tuple"""
        return self.name == ""

    # Not sure if this property serves any useful purpose - but it probably
    # shouldn't be user accessible so shouldn't be documented.
    @property
    def anonymous(self):
        return self._anon

    def canonical(self, key):
        """Returns the canonical name for a field"""
        return self._key2canon[key]

    def keys(self):
        """Returns the names of fields"""
        return self._byname.keys()

    @property
    def indexes(self):
        """Return the list of fields that have been specified as indexed"""
        return self._indexed_fields

    @indexes.setter
    def indexes(self,indexed_fields):
        if self._indexed_fields:
            raise RuntimeError(("Trying to reset the indexed fields for a "
                                "PredicateDefn doesn't make sense"))
        self._indexed_fields = tuple(indexed_fields)

    # Internal property
    @property
    def parent(self):
        """Return the Predicate/Complex-term associated with this definition"""
        return self._parent_cls

    # Internal property
    @parent.setter
    def parent(self, pc):
        if self._parent_cls:
            raise RuntimeError(("Trying to reset the parent for a "
                                "PredicateDefn doesn't make sense"))
        self._parent_cls = pc
        self._path_class = _define_predicate_path_subclass(pc)
#        self._path = seq = self._path_class([PathIdentity(pc,pc.__name__)])
        self._path = self._path_class([PathIdentity(pc,pc.__name__)])

    # Internal property
    @property
    def path(self): return self._path

    # Internal property
    @property
    def path_class(self): return self._path_class

    def alias(self, name=None):
        """Create an alias path for the predicate

        This lets the user create a join query between a predicate and itself.

        If a name is specified then a unique name is generated. Uses a component
        uuid4() as the unique number - aliases shouldn't be used often so
        using a small component should still be unlikely to clash.

        """
        if not name:
            classname = self._parent_cls.__name__
            num = uuid.uuid4().time_mid
            name = "({}.alias.{})".format(classname,num)
        return self._path_class([PathIdentity(self._parent_cls,name)])


    def __len__(self):
        '''Returns the number of fields'''
        return len(self._byidx)

    def __getitem__(self, key):
        '''Find a field by position index or by name'''
        try:
            idx = int(key)
            return self._byidx[idx]
        except ValueError as e:
            return self._byname[key]

    def __iter__(self):
        return iter(self._byidx)

# ------------------------------------------------------------------------------
# Helper function that performs some data conversion on a value to make it match
# a field's input. If the value is a tuple and the field definition is a
# complex-term then it tries to create an instance corresponding to the
# tuple. Otherwise simply returns the value.
# ------------------------------------------------------------------------------

def _preprocess_field_value(field_defn, v):
    predicate_cls = field_defn.complex
    if not predicate_cls: return v
    mt = predicate_cls.meta
    if isinstance(v, predicate_cls): return v
    if (mt.is_tuple and isinstance(v,Predicate) and v.meta.is_tuple) or \
       isinstance(v, tuple):
        if len(v) != len(mt):
            raise ValueError(("mis-matched arity between field {} (arity {}) and "
                             " value (arity {})").format(field_defn, len(mt), len(v)))
        return predicate_cls(*v)
    else:
        return v

# ------------------------------------------------------------------------------
# Helper functions for PredicateMeta class to create a Predicate
# class constructor.
# ------------------------------------------------------------------------------

# Construct a Predicate via an explicit (raw) clingo.Symbol object
def _predicate_init_by_raw(self, **kwargs):
    if len(kwargs) != 1:
        raise ValueError("Invalid combination of keyword arguments")
    raw = kwargs["raw"]
    self._raw = raw
    try:
        cls=type(self)
        if raw.type != clingo.SymbolType.Function: raise ValueError()
        arity=len(raw.arguments)
        if raw.name != cls.meta.name: raise ValueError()
        if arity != cls.meta.arity: raise ValueError()
        if cls.meta.sign is not None and cls.meta.sign != raw.positive: raise ValueError()
        self._field_values = tuple( f.defn.cltopy(raw.arguments[f.index]) \
                                     for f in self.meta )
    except (TypeError,ValueError):
        raise ValueError(("Failed to unify clingo.Symbol object {} with "
                          "Predicate class {}").format(raw, cls.__name__))

# Construct a Predicate via the field keywords
def _predicate_init_by_keyword_values(self, **kwargs):
    argnum=0
    self._field_values = []
    clingoargs = []
    for f in self.meta:
        if f.name in kwargs:
            v= _preprocess_field_value(f.defn, kwargs[f.name])
            argnum += 1
        elif f.defn.has_default:
            # Note: must be careful to get the default value only once in case
            # it is a function with side-effects.
            v = _preprocess_field_value(f.defn, f.defn.default)
        else:
            raise TypeError(("Missing argument for field \"{}\" (which has no "
                             "default value)").format(f.name))

        # Set the value for the field
        self._field_values.append(v)
        clingoargs.append(f.defn.pytocl(v))

    # Turn it into a tuple
    self._field_values = tuple(self._field_values)

    # Calculate the sign of the literal and check that it matches the allowed values
    if "sign" in kwargs:
        sign = bool(kwargs["sign"])
        argnum += 1
    else:
        sign = True

    if len(kwargs) > argnum:
        args=set(kwargs.keys())
        expected=set([f.name for f in self.meta])
        raise TypeError(("Unexpected keyword arguments for \"{}\" constructor: "
                          "{}").format(type(self).__name__, ",".join(args-expected)))
    if self.meta.sign is not None:
        if sign != self.meta.sign:
            raise ValueError(("Predicate {} is defined to only allow {} signed "
                              "instances").format(self.__class__, self.meta.sign))

    # Create the raw clingo.Symbol object
    self._raw = clingo.Function(self.meta.name, clingoargs, sign)

# Construct a Predicate using keyword arguments
def _predicate_init_by_positional_values(self, *args, **kwargs):
    argc = len(args)
    arity = len(self.meta)
    if argc != arity:
        raise ValueError("Expected {} arguments but {} given".format(argc,arity))

    clingoargs = []
    self._field_values = []
    for f in self.meta:
        v = _preprocess_field_value(f.defn, args[f.index])
        self._field_values.append(v)
        clingoargs.append(f.defn.pytocl(v))

    # Turn it into a tuple
    self._field_values = tuple(self._field_values)

    # Calculate the sign of the literal and check that it matches the allowed values
    sign = bool(kwargs["sign"]) if "sign" in kwargs else True
    if self.meta.sign is not None and sign != self.meta.sign:
        raise ValueError(("Predicate {} is defined to only allow {} "
                          "instances").format(type(self).__name__, self.meta.sign))

    # Create the raw clingo.Symbol object
    self._raw = clingo.Function(self.meta.name, clingoargs, sign)

# Constructor for every Predicate sub-class
def _predicate_constructor(self, *args, **kwargs):
    if len(args) > 0:
        if len(kwargs) > 1 or (len(kwargs) == 1 and "sign" not in kwargs):
            raise ValueError(("Invalid Predicate initialisation: only \"sign\" is a "
                             "valid keyword argument when combined with positional "
                              "arguments: {}").format(kwargs))
        _predicate_init_by_positional_values(self, *args,**kwargs)
    elif "raw" in kwargs:
        _predicate_init_by_raw(self, **kwargs)
    else:
        _predicate_init_by_keyword_values(self, **kwargs)

    if self.meta.is_tuple: self._hash = hash(tuple(self._field_values))
    else: self._hash = hash(self._raw)


def _predicate_base_constructor(self, *args, **kwargs):
    raise TypeError(("Predicate/ComplexTerm must be sub-classed"))

#------------------------------------------------------------------------------
# Metaclass constructor support functions to create the fields
#------------------------------------------------------------------------------

# Generate a default predicate name from the Predicate class name.
def _predicatedefn_default_predicate_name(class_name):

    # If first letter is lower-case then do nothing
    if class_name[0].islower(): return class_name

    # Otherwise, replace any sequence of upper-case only characters that occur
    # at the beginning of the string or immediately after an underscore with
    # lower-case equivalents. The sequence of upper-case characters can include
    # non-alphabetic characters (eg., numbers) and this will still be treated as
    # a single sequence of upper-case characters.  This covers basic naming
    # conventions: camel-case, snake-case, and acronyms.

    output=""
    incap=True
    for c in class_name:
        if c == '_': output += c ; incap = True ; continue
        if not c.isalpha(): output += c ; continue
        if not incap: output += c ; continue
        if c.isupper(): output += c.lower() ; continue
        else: output += c ; incap = False ; continue

    return output

# Detect a class definition for a ComplexTerm
def _is_complexterm_declaration(name,obj):
    if not inspect.isclass(obj): return False
    if not issubclass(obj,ComplexTerm): return False
    return obj.__name__ == name

# Detect a class definition that is not a ComplexTerm subclass or RawField
# subclass or named 'Meta'
def _is_bad_predicate_inner_class_declaration(name,obj):
    if not inspect.isclass(obj): return False
    if issubclass(obj,ComplexTerm): return False
    if issubclass(obj,RawField): return False
    if name == "Meta": return True
    return obj.__name__ == name


# build the metadata for the Predicate - NOTE: this funtion returns a
# PredicateDefn instance but it also modified the dct paramater to add the fields. It
# also checks to make sure the class Meta declaration is error free: 1) Setting
# a name is not allowed for a tuple, 2) Sign controls if we want to allow
# unification against a positive literal only, a negative literal only or
# both. Sign can be True/False/None. By default sign is None (meaning both
# positive/negative) unless it is a tuple then it is positive only.

def _make_predicatedefn(class_name, dct):

    # Set the default predicate name
    pname = _predicatedefn_default_predicate_name(class_name)
    anon = False
    sign = None
    is_tuple = False

    if "Meta" in dct:
        metadefn = dct["Meta"]
        if not inspect.isclass(metadefn):
            raise TypeError("'Meta' attribute is not an inner class")

        # What has been defined
        name_def = "name" in metadefn.__dict__
        is_tuple_def = "is_tuple" in metadefn.__dict__
        sign_def = "sign" in metadefn.__dict__

        if name_def : pname = metadefn.__dict__["name"]
        if is_tuple_def : is_tuple = bool(metadefn.__dict__["is_tuple"])
        if "_anon" in metadefn.__dict__:
            anon = metadefn.__dict__["_anon"]

        if name_def and not pname:
            raise ValueError(("Empty 'name' attribute is invalid. Use "
                              "'is_tuple=True' if you want to define a tuple."))
        if name_def and is_tuple:
            raise ValueError(("Cannot specify a 'name' attribute if "
                              "'is_tuple=True' has been set"))
        elif is_tuple: pname = ""

        if is_tuple: sign = True       # Change sign default if is tuple

        if "sign" in  metadefn.__dict__: sign = metadefn.__dict__["sign"]
        if sign is not None: sign = bool(sign)

        if is_tuple and not sign:
            raise ValueError(("Tuples cannot be negated so specifying "
                              "'sign' is None or False is invalid"))

    reserved = set(["meta", "raw", "clone", "sign", "Field"])

    # Generate the fields - NOTE: this relies on dct being an OrderedDict()
    # which is true from Python 3.5+ (see PEP520
    # https://www.python.org/dev/peps/pep-0520/)
    fas= []
    idx = 0

    for fname, fdefn in dct.items():

        # Ignore entries that are not field declarations
        if fname == "Meta": continue
        if _magic_name(fname): continue
        if _is_complexterm_declaration(fname, fdefn): continue
        if _is_bad_predicate_inner_class_declaration(fname, fdefn):
            raise TypeError(("Error defining class '{}': only ComplexTerm "
                             "sub-classes are allowed as inner classes of "
                             "a Predicate definition").format(fname))

        if fname in reserved:
            raise ValueError(("Error: invalid field name: '{}' "
                              "is a reserved keyword").format(fname))
        if fname.startswith('_'):
            raise ValueError(("Error: field names cannot start with an "
                              "underscore: {}").format(fname))
        try:
            fd = get_field_definition(fdefn)
            fa = FieldAccessor(fname, idx, fd)
            dct[fname] = fa
            fas.append(fa)
            idx += 1
        except TypeError as e:
            raise TypeError("Error defining field '{}': {}".format(fname,str(e)))

    # Create the "sign" attribute - must be assigned a parent in the metaclass
    # __init__() call.
    dct["sign"] = SignAccessor()

    # Now create the PredicateDefn object
    return PredicateDefn(name=pname,field_accessors=fas, anon=anon,sign=sign)

# ------------------------------------------------------------------------------
# Define a RawField sub-class that corresponds to a Predicate/ComplexTerm
# sub-class. This RawField sub-class will convert to/from a complex-term
# instances and clingo symbol objects.
# ------------------------------------------------------------------------------

def _define_field_for_predicate(cls):
    if not issubclass(cls, Predicate):
        raise TypeError(("Class {} is not a Predicate/ComplexTerm "
                         "sub-class").format(cls))

    field_name = "{}Field".format(cls.__name__)
    def _pytocl(v):
        if isinstance(v,cls): return v.raw
        if isinstance(v,tuple):
            if len(v) != len(cls.meta):
                raise ValueError(("incorrect values to unpack (expected "
                                  "{})").format(len(cls.meta)))
            try:
                v = cls(*v)
                return v.raw
            except Exception:
                raise TypeError(("Failed to unify tuple {} with complex "
                                  "term {}").format(v,cls))
        raise TypeError("Value {} ({}) is not an instance of {}".format(v,type(v),cls))

    def _cltopy(v):
        return cls(raw=v)

    field = type(field_name, (RawField,),
                 { "pytocl": _pytocl, "cltopy": _cltopy,
                   "complex": lambda self: cls})
    return field

#------------------------------------------------------------------------------
# A Metaclass for the Predicate base class
#------------------------------------------------------------------------------
class _PredicateMeta(type):

    #--------------------------------------------------------------------------
    # Allocate the new metaclass
    #--------------------------------------------------------------------------
    def __new__(meta, name, bases, dct):
        if name == "Predicate":
            dct["_predicate"] = None
            dct["__init__"] = _predicate_base_constructor
            return super(_PredicateMeta, meta).__new__(meta, name, bases, dct)

        # Create the metadata AND populate dct - the class dict (including the fields)

        # Set the _meta attribute and constuctor
        dct["_meta"] = _make_predicatedefn(name, dct)
        dct["__init__"] = _predicate_constructor
        dct["_field"] = _lateinit("{}._field".format(name))

        parents = [ b for b in bases if issubclass(b, Predicate) ]
        if len(parents) == 0:
            raise TypeError("Internal bug: number of Predicate bases is 0!")
        if len(parents) > 1:
            raise TypeError("Multiple Predicate sub-class inheritance forbidden")

        return super(_PredicateMeta, meta).__new__(meta, name, bases, dct)

    def __init__(cls, name, bases, dct):
        if name == "Predicate":
            return super(_PredicateMeta, cls).__init__(name, bases, dct)

        # Set a RawField sub-class that converts to/from cls instances
        dct["_field"].assign(_define_field_for_predicate(cls))

        md = dct["_meta"]
        # The property attribute for each field can only be created in __new__
        # but the class itself does not get created until after __new__. Hence
        # we have to set the pointer within the field back to the this class
        # here. Similar argument applies for generating the field indexes
        md.parent = cls
        for field in md:
            dct[field.name].parent = cls
        md.indexes=_get_paths_for_default_indexed_fields(cls)

        # Assign the parent for the SignAccessor
        dct["sign"].parent = cls

        return super(_PredicateMeta, cls).__init__(name, bases, dct)

    # A Predicate subclass is an instance of this meta class. So to
    # provide querying of a Predicate subclass Blah by a positional
    # argument we need to implement __getitem__ for the metaclass.
    def __getitem__(self, idx):
        return self.meta.path[idx]

    def __iter__(self):
        return iter([self[k] for k in self.meta.keys()])

#------------------------------------------------------------------------------
# A base non-logical symbol that all predicate/complex-term declarations must
# inherit from. The Metaclass creates the magic to create the fields and the
# underlying clingo.Symbol object.
# ------------------------------------------------------------------------------

class Predicate(object, metaclass=_PredicateMeta):
    """Encapsulates an ASP predicate or complex term in an easy to access object.

    This is the heart of the ORM model for defining the mapping of a complex
    term or predicate to a Python object. ``ComplexTerm`` is simply an alias for
    ``Predicate``.

    Example:
       .. code-block:: python

           class Booking(Predicate):
               date = StringField(index = True)
               time = StringField(index = True)
               name = StringField(default = "relax")

           b1 = Booking("20190101", "10:00")
           b2 = Booking("20190101", "11:00", "Dinner")

    Field names can be any valid Python variable name subject to the following
    restrictions:

    - it cannot start with a "_", or
    - it cannot be be one of the following reserved words: "meta", "raw",
      "clone", or "Field".

    The constructor creates a predicate instance (i.e., a *fact*) or complex
    term. If the ``raw`` parameter is used then it tries to unify the supplied
    Clingo.Symbol with the class definition, and will raise a ValueError if it
    fails to unify.

    Args:
      **kwargs:

         - if a single named parameter ``raw`` is specified then it will try to
           unify the parameter with the specification, or
         - named parameters corresponding to the field names.

    """

    #--------------------------------------------------------------------------
    #
    #--------------------------------------------------------------------------
    def __init__(self):
        raise NotImplementedError(("Class {} can only be instantiated through a "
                                   "sub-class").format(self.__name__))


    #--------------------------------------------------------------------------
    # Properties and functions for Predicate
    #--------------------------------------------------------------------------

    # Get the underlying clingo.Symbol object
    @property
    def raw(self):
        """Returns the underlying clingo.Symbol object"""
        return self._raw

#    # Get the sign of the literal
#    @property
#    def sign(self):
#        """Returns the sign of the predicate instance"""
#        return self._raw.positive

    @_classproperty
    def Field(cls):
        """A RawField sub-class corresponding to a Field for this class."""
        return cls._field

    # Clone the object with some differences
    def clone(self, **kwargs):
        """Clone the object with some differences.

        For any field name that is not one of the parameter keywords the clone
        keeps the same value. But for any field listed in the parameter keywords
        replace with specified new value.
        """

        # Sanity check
        clonekeys = set(kwargs.keys())
        objkeys = set(self.meta.keys())
        diffkeys = clonekeys - objkeys
        diffkeys.discard("sign")

        if diffkeys:
            raise ValueError("Unknown field names: {}".format(diffkeys))

        # Get the arguments for the new object
        cloneargs = {}
        if "sign" in clonekeys: cloneargs["sign"] = kwargs["sign"]
        for field in self.meta:
            if field.name in kwargs:
                cloneargs[field.name] = kwargs[field.name]
            else:
                cloneargs[field.name] = self._field_values[field.index]
                kwargs[field.name] = self._field_values[field.index]

        # Create the new object
        return type(self)(**cloneargs)

    #--------------------------------------------------------------------------
    # Class methods and properties
    #--------------------------------------------------------------------------

    # Get the metadata for the Predicate definition
    @_classproperty
    def meta(cls):
        """The meta data (definitional information) for the Predicate/Complex-term"""
        return cls._meta

    # Returns whether or not a clingo.Symbol object can unify with this
    # Predicate
    @classmethod
    def _unifies(cls, raw):
        if raw.type != clingo.SymbolType.Function: return False

        if raw.name != cls.meta.name: return False
        if len(raw.arguments) != len(cls.meta): return False

        if cls.meta.sign is not None:
            if cls.meta.sign != raw.positive: return False

        for idx, field in enumerate(cls.meta):
            if not field.defn.unifies(raw.arguments[idx]): return False
        return True

    # Factory that returns a unified Predicate object
    @classmethod
    def _unify(cls, raw):
        return cls(raw=raw)

    #--------------------------------------------------------------------------
    # Overloaded index operator to access the values and len operator
    #--------------------------------------------------------------------------

    def __iter__(self):
        # The number of parameters in a predicate are always small so convenient
        # to generate a list of values rather than have a specialised iterator.
        return iter([self[idx] for idx in range(0,len(self))])

    def __getitem__(self, idx):
        """Allows for index based access to field elements."""
        return self.meta[idx].__get__(self)

    def __bool__(self):
        '''Behaves like a tuple: returns False if the predicate/complex-term has no elements'''
        return len(self.meta) > 0

    def __len__(self):
        '''Returns the number of fields in the object'''
        return len(self.meta)

    #--------------------------------------------------------------------------
    # Overload the unary minus operator to return the complement of this literal
    # (if its positive return a negative equivaent and vice-versa)
    # --------------------------------------------------------------------------
    def __neg__(self):
        return self.clone(sign=not self.sign)

    #--------------------------------------------------------------------------
    # Overloaded operators
    #--------------------------------------------------------------------------
    def __eq__(self, other):
        """Overloaded boolean operator."""
        if isinstance(other, self.__class__): return self.raw == other.raw
        if self.meta.is_tuple:
            return self._field_values == other
        elif not isinstance(other, Predicate):
            return NotImplemented
        return False

    def __ne__(self, other):
        """Overloaded boolean operator."""
        result = self.__eq__(other)
        if result is NotImplemented: return NotImplemented
        return not result

    def __lt__(self, other):
        """Overloaded boolean operator."""

        # If it is the same predicate class then compare individual fields
        if isinstance(other, self.__class__):

             # Negative literals are less than positive literals
            if self.raw.positive != other.raw.positive:
                return self.raw.positive < other.raw.positive

            # compare each field in order
            for idx in range(0,len(self._meta)):
                selfv = self[idx]
                otherv = other[idx]
                if selfv == otherv: continue
                return selfv < otherv
            return False

        # If different predicates then compare the raw value
        elif isinstance(other, Predicate):
            return self.raw < other.raw

        # Else an error
        return NotImplemented

    def __ge__(self, other):
        """Overloaded boolean operator."""
        result = self.__lt__(other)
        if result is NotImplemented: return NotImplemented
        return not result

    def __gt__(self, other):
        """Overloaded boolean operator."""

        # If it is the same predicate class then compare individual fields
        if isinstance(other, self.__class__):
            # Positive literals are greater than negative literals
            if self.raw.positive != other.raw.positive:
                return self.raw.positive > other.raw.positive

            # compare each field in order
            for idx in range(0,len(self._meta)):
                selfv = self[idx]
                otherv = other[idx]
                if selfv == otherv: continue
                return selfv > otherv
            return False

        # If different predicates then compare the raw value
        if not isinstance(other, Predicate):
            return self.raw > other.raw

        # Else an error
        return NotImplemented

    def __le__(self, other):
        """Overloaded boolean operator."""
        result = self.__gt__(other)
        if result is NotImplemented: return NotImplemented
        return not result

    def __hash__(self):
        return self._hash
        if self.meta.is_tuple:
            return hash(self._field_values)
        else:
            return hash(self._raw)

    def __str__(self):
        """Returns the Predicate as the string representation of the raw
        clingo.Symbol.
        """
        return str(self.raw)

    def __repr__(self):
        return self.__str__()

#------------------------------------------------------------------------------
# Predicate and ComplexTerm are simply aliases for Predicate.
#------------------------------------------------------------------------------

ComplexTerm=Predicate

#------------------------------------------------------------------------------
# A function for defining Predicate sub-classes containing only RawField
# parameters. Useful when debugging ASP code and you just want to use the class
# for easy display/printing.
# ------------------------------------------------------------------------------

def simple_predicate(*args):
    """Factory function to define a predicate with only RawField arguments.

    A helper factory function that takes a name and an arity and returns a
    predicate class that is suitable for unifying with predicate instances of
    that name and arity. It's parameters are all specified as RawFields.

    This function is useful for debugging ASP programs. There may be some
    auxillary predicates that you aren't interested in extracting their values
    but instead you simply want to print them to the screen in some order.

    The function must be called using positional arguments with either 2 or 3
    arguments. For the 3 argument case a class name is specified for the name of
    the new predicate. For the 2 argument case an anonymous predicate class name
    is automatically generated.

    Args:
       optional subclass_name: new sub-class name (anonymous if none specified).
       name: the name of the predicate to match against
       arity: the arity for the predicate

    """
    largs = len(args)
    if largs == 2:
        subclass_name = "ClormAnonPredicate"
        name = args[0]
        arity = args[1]
    elif largs == 3:
        subclass_name = args[0]
        name = args[1]
        arity = args[2]
    else:
        raise TypeError("simple_predicate() missing required positional arguments")

    # Use an OrderedDict to ensure the correct order of the field arguments
    proto = collections.OrderedDict([("arg{}".format(i+1), RawField())
                                     for i in range(0,arity)])
    proto['Meta'] = type("Meta", (object,),
                         {"name" : name, "is_tuple" : False, "_anon" : True})
    return type("ClormAnonPredicate", (Predicate,), proto)





#------------------------------------------------------------------------------
# Internal supporting functions
# ------------------------------------------------------------------------------

#------------------------------------------------------------------------------
# Helper function to check if all the paths in a collection are root paths and
# return path objects.
# ------------------------------------------------------------------------------
def validate_root_paths(paths):
    def checkroot(p):
        p = path(p)
        if not p.meta.is_root:
            raise ValueError("'{}' in '{}' is not a root path".format(p,paths))
        return p
    return list(map(checkroot,paths))





#------------------------------------------------------------------------------
# main
#------------------------------------------------------------------------------
if __name__ == "__main__":
    raise RuntimeError('Cannot run modules')
