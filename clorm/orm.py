# -----------------------------------------------------------------------------
# ORM provides a Object Relational Mapper type model for specifying non-logical
# symbols (ie., predicates and terms)
# ------------------------------------------------------------------------------

#import logging
#import os
import io
import contextlib
import inspect
import operator
import collections
import bisect
import abc
import functools
import clingo
import typing

__all__ = [
    'RawField',
    'IntegerField',
    'StringField',
    'ConstantField',
    'refine_field',
    'Placeholder',
    'NonLogicalSymbol',
    'Predicate',
    'ComplexTerm',
    'desc',
    'unify',
    'FactBase',
    'FactBaseBuilder',
    'ph_',
    'ph1_',
    'ph2_',
    'ph3_',
    'ph4_',
    'not_',
    'and_',
    'or_',
    'TypeCastSignature',
    'make_function_asp_callable',
    'make_method_asp_callable'
    ]

#------------------------------------------------------------------------------
# Global
#------------------------------------------------------------------------------
#g_logger = logging.getLogger(__name__)

#------------------------------------------------------------------------------
# A _classproperty decorator. (see https://stackoverflow.com/questions/3203286/how-to-create-a-read-only-class-property-in-python)
#------------------------------------------------------------------------------
class _classproperty(object):
    def __init__(self, getter):
        self.getter= getter
    def __get__(self, instance, owner):
        return self.getter(owner)

#------------------------------------------------------------------------------
# A property to help with delayed initialisation. Useful for metaclasses where
# an object needs to be created in the __new__ call but can only be assigned in
# the __init__ call.
# ------------------------------------------------------------------------------
class _lateinit(object):
    def __init__(self, value):
        self._value=value
    def assign(self, value):
        self._value=value
    def __get__(self, instance, owner):
        return self._value

#------------------------------------------------------------------------------
# Field Path specification. It gives a path to an individual field within a
# predicate/complex-term. This specifies the chain of links for a field.  For
# example, a Pred.a.b for a predicate Pred with a field a an a subfield b.  From
# the FieldPath you can uniquely (recursively) identify a field.  It is used for
# querying of fields and sub-fields as well as for specifying indexes for a
# FactBase.
# ------------------------------------------------------------------------------

# A link in the field path
FieldPathLink = collections.namedtuple('FieldPathLink', 'defn key')

class FieldPath(object):

    #--------------------------------------------------------------------------
    # The initialiser validates the chain and computes a canonical version
    #--------------------------------------------------------------------------
    def __init__(self, chain):
        if not chain: raise ValueError("An empty FieldPathBuilder spec is invalid")
        self._spec = []
        self._canon = []

        # validate and build the specifcation and a canonical version
        new_field = None
        last_idx = len(chain)-1
        for idx, (field, key) in enumerate(chain):
            if not issubclass(field, RawField):
                raise ValueError(("Element {} in {} is not a field "
                                  "definition").format(field, chain))
            if idx < last_idx:
                if not field.complex:
                    raise ValueError(("Field {} (number {}) in {} is not "
                                  "complex").format(field, idx, chain))
            if idx == last_idx and key is not None:
                raise ValueError(("The last key element {} in {} must be "
                                  "None").format(key, chain))
            if idx > 0 and field != new_field:
                raise ValueError(("Field {} didn't match expectation "
                                  "{} in {}").format(field, new_field, chain))

            canon = key
            if idx < last_idx:
                nls_defn = field.complex.meta
                error=False
                try:
                    new_field = type(nls_defn[key].defn)
                    canon = nls_defn.canonical(key)
                except IndexError:
                    error=True
                except ValueError:
                    error=True
                if error:
                    raise ValueError(("Key {} is not valid for field {} in "
                                      "{}").format(key, field, chain))
            # Update the specification
            self._spec.append(FieldPathLink(field,key))
            self._canon.append(FieldPathLink(field,canon))

        # Turn the spec into a tuple
        self._spec = tuple(self._spec)
        self._canon = tuple(self._canon)

    #--------------------------------------------------------------------------
    # Returns the predicate associated with the fieldpath. Note: assumes that it
    # is an absolute FieldPath where the first element does indeed reference a
    # predicate.
    # --------------------------------------------------------------------------
    @property
    def predicate(self):
        return self._canon[0].defn.complex

    @property
    def defn(self):
        return self._canon[-1].defn

    def canonical(self):
        return FieldPath(self._canon)

    #--------------------------------------------------------------------------
    # Equality overload
    #--------------------------------------------------------------------------
    def __eq__(self, other):
        # Since we can refer to elements by either index or attribute use the
        # canonoical version to guarantee semantic equivalence
        if not isinstance(other, self.__class__): return NotImplemented
        result = self._canon == other._canon
        return result

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented: return NotImplemented
        return not result

    #--------------------------------------------------------------------------
    # Access the elements in the spec
    #--------------------------------------------------------------------------
    def __getitem__(self, idx):
        return self._spec[idx]

    def __iter__(self):
        return iter(self._spec)

    def __len__(self):
        return len(self._spec)

    #--------------------------------------------------------------------------
    # Other functions
    #--------------------------------------------------------------------------
    def __hash__(self):
        return hash(self._canon)

    def __str__(self):
        tmp = self.predicate.__name__
        for f in self._spec[:-1]:
            try:
                fi = int(f.key)
                tmp += "[{}]".format(fi)
            except:
                tmp += ".{}".format(f.key)
        return tmp

#------------------------------------------------------------------------------
# FieldPathBuilder and supporting functions and classes. The builder is the key
# because allows for the nice user syntax (eg., Pred.a.b or Pred.a[0]). It does
# this by creating a builder for every NonLogicalSymbol (NLS) sub-class where
# the corresponding builder creates a attribute for each field names that in
# turns returns the approriate builder object - a chain of field references.
# The FieldPathBuilder also overloads the boolean comparison operator to return
# a comparator. This allows for the nice syntax such as, Pred.a.b == 1
# ------------------------------------------------------------------------------

def _make_fpb_class(field_defn):
    class_name = field_defn.__name__ + "FPB"
    return type(class_name, (FieldPathBuilder,), { "_field_defn" : field_defn })

def _fpb_base_constructor(self, *args, **kwargs):
    raise TypeError("FieldPathBuilder must be sub-classed")

def _fpb_subclass_constructor(self, prev=None, key=None):
    self._chain = []
    if prev: self._chain = list(prev._chain)
    self._chain.append((self._field_defn, key))
    self._meta = FieldPathBuilder.Meta(self)

def _fpb_make_field_access(name,defn):
    def access(self):
        return defn.FieldPathBuilder(self, name)
    return access

class _FieldPathBuilderMeta(type):
    def __new__(meta, name, bases, dct):
        dct["_byidx"] = []
        dct["_byname"] = {}

        if name == "FieldPathBuilder":
            dct["__init__"] = _fpb_base_constructor
            return super(_FieldPathBuilderMeta, meta).__new__(meta, name, bases, dct)

        dct["__init__"] = _fpb_subclass_constructor

        # Expecting "_field_defn" to be defined
        field_defn = dct["_field_defn"]
        nls = field_defn.complex
        if nls:
            for field in nls.meta:
                dct[field.name] = property(_fpb_make_field_access(field.name,field.defn))
                dct["_byidx"].append(field)
                dct["_byname"][field.name] = field

        # The appropriate fields have been created
        return super(_FieldPathBuilderMeta, meta).__new__(meta, name, bases, dct)


class FieldPathBuilder(object, metaclass=_FieldPathBuilderMeta):

    #--------------------------------------------------------------------------
    # A wrapper to provide some useful functions/properties in a sub-namespace
    #--------------------------------------------------------------------------

    class Meta(object):
        def __init__(self, fpb):
            self._fpb = fpb

        #--------------------------------------------------------------------------
        # Return the FieldPath generated by the builder - using the function name
        # meta because this is a reserved keyword so guaranteed not to be an
        # attribute.
        # --------------------------------------------------------------------------
        def field_path(self):
            return FieldPath(self._fpb._comp_list())

        #--------------------------------------------------------------------------
        # Return the a FieldOrderBy structure for ascending and descending
        # order. Used by the Select query.
        # --------------------------------------------------------------------------
        def asc(self):
            return FieldOrderBy(self.field_path(), asc=True)
        def desc(self):
            return FieldOrderBy(self.field_path(), asc=False)

    #--------------------------------------------------------------------------
    # Support function to take the internal chain of the FieldClassBuilder and
    # turn it into a list of class,name matching pairs, where the last element
    # only contains the type (with None) for the name element.
    # --------------------------------------------------------------------------
    def _comp_list(self):
        if not self._chain: return []
        if len(self._chain) == 1: return list(self._chain)
        values = []
        for i in range(0,len(self._chain)-1):
            values.append((self._chain[i][0], self._chain[i+1][1]))
        values.append((self._chain[-1][0],None))
        return values

    #--------------------------------------------------------------------------
    # Return the underlying meta object with useful functions
    #--------------------------------------------------------------------------
    @property
    def meta(self):
        return self._meta

    #--------------------------------------------------------------------------
    # Helper functor to generate a FieldQueryComparator based on a query
    # specification
    # --------------------------------------------------------------------------
    def _make_fq_comparator(self, op, other):
        if isinstance(other, FieldPathBuilder):
            other = _FieldPathEval(other.meta.field_path())
        return FieldQueryComparator(op, _FieldPathEval(self.meta.field_path()), other)

    #--------------------------------------------------------------------------
    # Allow lookup of fields by name or index
    # --------------------------------------------------------------------------
    def __getitem__(self, key):
        '''Find a field by position index or by name'''
        try:
            key = int(key)
            field = self._byidx[key]
        except ValueError as e:
            field = self._byname[key]
        # Return a new FieldPathBuilder instance with self as the previous link in
        # the chain.
        return field.defn.FieldPathBuilder(self, key)

    #--------------------------------------------------------------------------
    # Overload the boolean operators to return a functor
    #--------------------------------------------------------------------------
    def __eq__(self, other):
        return self._make_fq_comparator(operator.eq, other)
    def __ne__(self, other):
        return self._make_fq_comparator(operator.ne, other)
    def __lt__(self, other):
        return self._make_fq_comparator(operator.lt, other)
    def __le__(self, other):
        return self._make_fq_comparator(operator.le, other)
    def __gt__(self, other):
        return self._make_fq_comparator(operator.gt, other)
    def __ge__(self, other):
        return self._make_fq_comparator(operator.ge, other)

    def __str__(self):
        if len(self._chain) < 1: return "FieldPathBuilder(<partial>)"
        return str(self.meta.field_path())

#------------------------------------------------------------------------------
# FieldPathEval evaluates a FieldPath with respect to a fact (a predicate
# instance).  It is a functor that extracts a component from a fact based on a
# specification of the class/attribute. Used by the FieldQueryComparator for
# querying.
# ------------------------------------------------------------------------------

class _FieldPathEval(object):
    def __init__(self, fpspec):
        if not isinstance(fpspec, FieldPath):
            raise TypeError("{} is not of type {}".format(fpspec, FieldPath))
        self._fpspec = fpspec

    def __call__(self, fact):
        if type(fact) != self._fpspec.predicate:
            raise TypeError(("Fact {} is not of type "
                             "{}").format(fact, self._fpspec.predicate))
        value = fact
        for field,key in self._fpspec:
            if key == None: return value
            value = field.complex.__getitem__(value, key)
        raise ValueError(("Internal error: invalid FPGet specification:"
                          "{}").format(self._fpspec))

    @property
    def spec(self): return self._fpspec

#------------------------------------------------------------------------------
# RawField class captures the definition of a term between python and clingo. It is
# not meant to be instantiated.
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

def _sfm_constructor(self, default=None, index=False):
    """Default values"""
    self._default = default
    self._index = index

    # Check that the default is a valid value
    if default:
        try:
            self.pytocl(default)
        except TypeError:
            raise TypeError("Invalid default value \"{}\" for {}".format(
                default, type(self).__name__))

class _RawFieldMeta(type):
    def __new__(meta, name, bases, dct):

        # Add a default initialiser if one is not already defined
        if "__init__" not in dct:
            dct["__init__"] = _sfm_constructor

        dct["_fpb"] = _lateinit(None)

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
            raise TypeError("Multiple RawField sub-class inheritance forbidden")
        dct["_parentclass"] = parents[0]

        # When a conversion is not specified raise a NotImplementedError
        def _raise_nie(cls,v):
            raise NotImplementedError("No implemented conversion")

        if "cltopy" in dct:
            dct["cltopy"] = classmethod(_make_cltopy(dct["cltopy"]))
        else:
            dct["cltopy"] = classmethod(_raise_nie)

        if "pytocl" in dct:
            dct["pytocl"] = classmethod(_make_pytocl(dct["pytocl"]))
        else:
            dct["pytocl"] = classmethod(_raise_nie)


        # For complex-terms provide an interface to the underlying complex term
        # object as well as the appropriate FieldPathBuilder.
        if "complex" in dct:
            dct["complex"] = _classproperty(dct["complex"])
        else:
            dct["complex"] = _classproperty(lambda cls: None)
#            dct["complex"] = _classproperty(None)

        return super(_RawFieldMeta, meta).__new__(meta, name, bases, dct)

    def __init__(cls, name, bases, dct):
        dct["_fpb"].assign(_make_fpb_class(cls))

        return super(_RawFieldMeta, cls).__init__(name, bases, dct)

#------------------------------------------------------------------------------
# Field definitions. All fields have the functions: pytocl, cltopy,
# and unifies, and the property: default
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
       object and is must output a Python string object.

    Args:
      default: A default value when instantiating a ``Predicate`` or
        ``ComplexTerm`` object. Defaults to ``None``.
      index (bool): Determine if this field should be indexed by default in a
        ``FactBase```. Defaults to ``False``.

    """

    @classmethod
    def cltopy(cls, v):
        """Called when translating data from a Clingo to Python"""
        return v

    @classmethod
    def pytocl(cls, v):
        """Called when translating data from a Python to Clingo"""
        return v

    @classmethod
    def unifies(cls, v):
        """Returns whether a `Clingo.Symbol` can be unified with this type of term"""
        try:
            cls.cltopy(v)
        except TypeError:
            return False
        return True

    @_classproperty
    def complex(cls):
        return None

    @_classproperty
    def FieldPathBuilder(cls):
        return cls._fpb

    @property
    def default(self):
        """Returns the specified default value for the term (or None)"""
        return self._default

    @property
    def index(self):
        """Returns whether this field should be indexed by default in a `FactBase`"""
        return self._index

#------------------------------------------------------------------------------
# The three RawField
#------------------------------------------------------------------------------

class StringField(RawField):
    """A field to convert between a Clingo.String object and a Python string."""
    def _string_cltopy(raw):
        if raw.type != clingo.SymbolType.String:
            raise TypeError("Object {0} is not a clingo.String symbol")
        return raw.string

    cltopy = _string_cltopy
    pytocl = lambda v: clingo.String(v)

class IntegerField(RawField):
    """A field to convert between a Clingo.Number object and a Python integer."""
    def _integer_cltopy(raw):
        if raw.type != clingo.SymbolType.Number:
            raise TypeError("Object {0} is not a clingo.Number symbol")
        return raw.number

    cltopy = _integer_cltopy
    pytocl = lambda v: clingo.Number(v)

class ConstantField(RawField):
    """A field to convert between a simple Clingo.Function object and a Python
    string.

    """
    def _constant_cltopy(raw):
        if   (raw.type != clingo.SymbolType.Function or
              not raw.name or len(raw.arguments) != 0):
            raise TypeError("Object {0} is not a Simple symbol")
        return raw.name

    cltopy = _constant_cltopy
    pytocl = lambda v: clingo.Function(v,[])

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
        except TypeError:
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
    """Helper function to define a field sub-class that restricts the set of values.

    A helper function to define a sub-class of a RawField (or sub-class) that
    restricts the allowable values. For example, if you have a constant in a
    predicate that is restricted to the days of the week ("monday", ...,
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
    the new field. For the 2 argument case the field class name is automatically
    generated.

    Example:
       .. code-block:: python

           WorkDayField = refine_field(ConstantField,
              ["monday", "tuesday", "wednesday", "thursday", "friday"])

    Args:
       subclass_name: the name of the new sub-class (name generated if none specified).
       field_class: the field that is being sub-classed
       values or value functor: a list of values or a functor to determine validity

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
# Specification of an ordering over a field of a predicate/complex-term
#------------------------------------------------------------------------------
class FieldOrderBy(object):
    def __init__(self, fp, asc):
        self._fp = _to_field_path(fp)
        self._fpeval = _FieldPathEval(self._fp)
        self.asc = asc
    def compare(self, a,b):
        va = self._fpeval(a)
        vb = self._fpeval(b)
        if  va == vb: return 0
        if self.asc and va < vb: return -1
        if not self.asc and va > vb: return -1
        return 1

    def __str__(self):
        return "FieldOrderBy(field={},asc={})".format(self._fp, self.asc)

#------------------------------------------------------------------------------
# A helper function to return a FieldOrderBy descending object. Input is a
# FieldPathBuilder.
# ------------------------------------------------------------------------------
def desc(fpb):
    return fpb.meta.desc()

#------------------------------------------------------------------------------
# FieldAccessor - a Python descriptor (similar to a property) to access the
# value associated with a field. If called by the class then generates a
# FieldPathBuilder (that can be used to specify a query).  It has a __get__
# overload to return the data of the field if the function is called from an
# instance.
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
        self._parent_cls = pc

    def fpb(self):
        fpb_parent = self._parent_cls.Field.FieldPathBuilder(None,None)
        fpb = self._defn.FieldPathBuilder(fpb_parent, self._name)
        return fpb

    def __get__(self, instance, owner=None):
        if not instance: return self.fpb()

        if not isinstance(instance, self._parent_cls):
            raise TypeError(("field {} doesn't match type "
                             "{}").format(self, type(instance).__name__))
        return instance._term_values[self._name]
#            return term_defn.cltopy(self._raw.arguments[idx])

    def __set__(self, instance, value):
        raise AttributeError("field is a read-only data descriptor")



#------------------------------------------------------------------------------
# Helper function to cleverly handle a term definition. If the input is an
# instance of a RawField then simply return the object. If it is a subclass of
# RawField then return an instantiation of the object. If it is a tuple then
# treat it as a recursive definition and return an instantiation of a
# dynamically created complex-term corresponding to a tuple (which we call a
# Clorm tuple).
# ------------------------------------------------------------------------------

def _get_field_defn(defn):
    if inspect.isclass(defn):
        if not issubclass(defn,RawField):
            raise TypeError("Unrecognised field definition object {}".format(defn))
        return defn()

    # Simplest case of a RawField instance
    if isinstance(defn,RawField): return defn

    # Expecting a tuple and treat it as a recursive definition
    if not isinstance(defn, tuple):
        raise TypeError("Unrecognised field definition object {}".format(defn))

    proto = { "arg{}".format(i+1) : _get_field_defn(d) for i,d in enumerate(defn) }
    proto['Meta'] = type("Meta", (object,), {"istuple" : True, "_anon" : True})
    ct = type("AnonymousClormTuple", (NonLogicalSymbol,), proto)
    return ct.Field()


#------------------------------------------------------------------------------
# The NonLogicalSymbol base class and supporting functions and classes
# ------------------------------------------------------------------------------

#--------------------------------------------------------------------------
# One NLSDefn object for each NonLogicalSymbol sub-class
#--------------------------------------------------------------------------
class NLSDefn(object):
    """Encapsulates some meta-data for a NonLogicalSymbol (NLS) definition. Each NLS
    class will have a corresponding NLSDefn object that provides some
    specifies some introspective properties of the predicate/complex-term.

    """

    def __init__(self, name, fields, anon=False):
        self._name = name
        self._byidx = tuple(fields)
        self._byname = { f.name : f for f in fields }
        self._anon = anon
        self._key2canon = { f.index : f.name for f in fields }
        self._key2canon.update({f.name : f.name for f in fields })
        self._parent_cls = None

    @property
    def name(self):
        """Returns the string name of the predicate or complex term"""
        return self._name

    @property
    def is_tuple(self):
        """Returns true if the definition corresponds to a tuple"""
        return self.name == ""

    @property
    def anonymous(self):
        """Returns whether definition is anonymous or explicitly user created"""
        return self._anon

    def canonical(self, key):
        return self._key2canon[key]

    def keys(self):
        """Returns the names of fields"""
        return self._byname.keys()

    @property
    def parent(self):
        return self._parent_cls

    @parent.setter
    def parent(self, pc):
        self._parent_cls = pc

    def fpb(self):
        return self._parent_cls.Field.FieldPathBuilder(None,None)

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
# Helper function that takes a field definition and a value and if the value is
# a tuple and the field definition is a complex-term for a tuple then creates an
# instance corresponding to the tuple.
# ------------------------------------------------------------------------------

def _preprocess_field_input(field_defn, v):
    complex_term = field_defn.complex
    if not complex_term: return v
    if not isinstance(v,tuple): return v
    ctm = complex_term.meta
    if len(v) != len(ctm):
        raise ValueError("incorrect values to unpack (expected {})".format(len(ctm)))
    return complex_term(*v)
#    if not complex_term.meta.is_tuple:

# ------------------------------------------------------------------------------
# Helper functions for NonLogicalSymbolMeta class to create a NonLogicalSymbol
# class constructor.
# ------------------------------------------------------------------------------

# Construct a NonLogicalSymbol via an explicit (raw) clingo.Symbol object
def _nls_init_by_raw(self, **kwargs):
    if len(kwargs) != 1:
        raise ValueError("Invalid combination of keyword arguments")
    raw = kwargs["raw"]
    class_name = type(self).__name__
    if not self._unifies(raw):
        raise ValueError(("Failed to unify clingo.Symbol object {} with "
                          "NonLogicalSymbol class {}").format(raw, class_name))
    self._raw = raw
    for idx, f in enumerate(self.meta):
        self._term_values[f.name] = f.defn.cltopy(raw.arguments[idx])

# Construct a NonLogicalSymbol via the term keywords
def _nls_init_by_keyword_values(self, **kwargs):
    class_name = type(self).__name__
    pred_name = self.meta.name
    names = set(self.meta.keys())

    invalids = [ k for k in kwargs if k not in names ]
    if invalids:
        raise ValueError(("Arguments {} are not valid field names "
                          "of {}".format(invalids,class_name)))

    # Construct the clingo function arguments
    for field in self.meta:
        if field.name not in kwargs:
            if not field.defn.default:
                raise ValueError(("Unspecified term {} has no "
                                  "default value".format(field.name)))
            self._term_values[field.name] = _preprocess_field_input(
                field.defn, field.defn.default)
        else:
            self._term_values[field.name] = _preprocess_field_input(
                field.defn, kwargs[field.name])

    # Create the raw clingo.Symbol object
    self._raw = self._generate_raw()

# Construct a NonLogicalSymbol via the term keywords
def _nls_init_by_positional_values(self, *args):
    class_name = type(self).__name__
    pred_name = self.meta.name
    argc = len(args)
    arity = len(self.meta)
    if argc != arity:
        raise ValueError("Expected {} arguments but {} given".format(arity,argc))

    for idx, field in enumerate(self.meta):
        self._term_values[field.name] = _preprocess_field_input(field.defn, args[idx])

    # Create the raw clingo.Symbol object
    self._raw = self._generate_raw()

# Constructor for every NonLogicalSymbol sub-class
def _nls_constructor(self, *args, **kwargs):
    self._raw = None
    self._term_values = {}
    if "raw" in kwargs:
        _nls_init_by_raw(self, **kwargs)
    elif len(args) > 0:
        _nls_init_by_positional_values(self, *args)
    else:
        _nls_init_by_keyword_values(self, **kwargs)

def _nls_base_constructor(self, *args, **kwargs):
    raise TypeError("NonLogicalSymbol must be sub-classed")

#------------------------------------------------------------------------------
# Metaclass constructor support functions to create the terms
#------------------------------------------------------------------------------

# build the metadata for the NonLogicalSymbol - NOTE: this funtion returns a
# NLSDefn instance but it also modified the dct paramater to add the fields.
def _make_nlsdefn(class_name, dct):

    # Generate a default name for the NonLogicalSymbol
    name = class_name[:1].lower() + class_name[1:]  # convert first character to lowercase
    anon = False
    if "Meta" in dct:
        metadefn = dct["Meta"]
        if not inspect.isclass(metadefn):
            raise TypeError("'Meta' attribute is not an inner class")
        name_def="name" in metadefn.__dict__
        istuple_def="istuple" in metadefn.__dict__
        if name_def : name = metadefn.__dict__["name"]
        istuple = metadefn.__dict__["istuple"] if istuple_def else False
        if "_anon" in metadefn.__dict__:
            anon = metadefn.__dict__["_anon"]

        if name_def and istuple:
            raise AttributeError(("Mutually exclusive meta attibutes "
                                  "'name' and 'istuple' "))
        elif istuple: name = ""


    reserved = set(["meta", "raw", "clone", "Field"])

    # Generate the terms - NOTE: relies on dct being an OrderedDict()
    terms = []
    idx = 0
    for field_name, field_defn in dct.items():
        if field_name in reserved:
            raise ValueError(("Error: invalid term name: '{}' "
                              "is a reserved keyword").format(field_name))
        try:
            fd = _get_field_defn(field_defn)
            if field_name.startswith('_'):
                raise ValueError(("Error: term names cannot start with an "
                                  "underscore: {}").format(field_name))
            term = FieldAccessor(field_name, idx, fd)
            dct[field_name] = term
            terms.append(term)
            idx += 1
        except TypeError as e:
            # If we get here assume that the dictionary item is for something
            # other than a field definition.
            pass

    # Now create the NLSDefn object
    return NLSDefn(name=name,fields=terms, anon=anon)

#------------------------------------------------------------------------------
# A container to dynamically generate a RawField subclass corresponding to a
# Predicate/Complex-term class.
# ------------------------------------------------------------------------------
class _FieldContainer(object):
    def __init__(self):
        self._defn = None
    def set_defn(self, cls):
        field_defn_name = "{}Field".format(cls.__name__)
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
            raise TypeError("Value {} not an instance of {}".format(v, cls))

        def _cltopy(v):
            return cls(raw=v)

        self._defn = type(field_defn_name, (RawField,),
                          { "pytocl": _pytocl,
                            "cltopy": _cltopy,
                            "complex": lambda self: cls})
    @property
    def defn(self):
        return self._defn


#------------------------------------------------------------------------------
# A Metaclass for the NonLogicalSymbol base class
#------------------------------------------------------------------------------
class _NonLogicalSymbolMeta(type):

    #--------------------------------------------------------------------------
    # Allocate the new metaclass
    #--------------------------------------------------------------------------
    def __new__(meta, name, bases, dct):
        if name == "NonLogicalSymbol":
            dct["_nls"] = None
            dct["__init__"] = _nls_base_constructor
            dct["_meta"] = NLSDefn(name="",fields=[], anon=False) # make autodoc happy
            return super(_NonLogicalSymbolMeta, meta).__new__(meta, name, bases, dct)

        # Create the metadata AND populate dct - the class dict (including the terms)
        md = _make_nlsdefn(name, dct)

        # Set the _meta attribute and constuctor
        dct["_meta"] = md
        dct["__init__"] = _nls_constructor
        dct["_fieldcontainer"] = _FieldContainer()

        parents = [ b for b in bases if issubclass(b, NonLogicalSymbol) ]
        if len(parents) == 0:
            raise TypeError("Internal bug: number of NonLogicalSymbol bases is 0!")
        if len(parents) > 1:
            raise TypeError("Multiple NonLogicalSymbol sub-class inheritance forbidden")

        return super(_NonLogicalSymbolMeta, meta).__new__(meta, name, bases, dct)

    def __init__(cls, name, bases, dct):
        if name == "NonLogicalSymbol":
            return super(_NonLogicalSymbolMeta, cls).__init__(name, bases, dct)

        # Set this class as the field
        dct["_fieldcontainer"].set_defn(cls)

        md = dct["_meta"]
        # The property attribute for each term can only be created in __new__
        # but the class itself does not get created until after __new__. Hence
        # we have to set the pointer within the term back to the this class
        # here.
        md.parent = cls
        for field in md:
            dct[field.name].parent = cls

        return super(_NonLogicalSymbolMeta, cls).__init__(name, bases, dct)

    # A NonLogicalSymbol subclass is an instance of this meta class. So to
    # provide querying of a NonLogicalSymbol subclass Blah by a positional
    # argument we need to implement __getitem__ for the metaclass.
    def __getitem__(self, idx):
        fpb_parent = self.Field.FieldPathBuilder(None,None)
        fpb = self.meta[idx].defn.FieldPathBuilder(fpb_parent, idx)
        return fpb

    # Allow iterating over the fields
#    def __iter__(self):
#        '''Iterate through the fields of the Predicate/Complex-term'''
#        print("DPR CALLED HERE 2")
#        return iter(self.meta)

    # Also overload the __len__ function to return the arity of the
    # NonLogicalSymbol class when called from len().
#    def __len__(self):
#        '''Return the number of fields in the Predicate/Complex-term'''
#        raise TypeError("HERE")
#        print("DPR CALLED HERE 3")
#        return len(self.meta)

#------------------------------------------------------------------------------
# A base non-logical symbol that all predicate/term declarations must inherit
# from. The Metaclass creates the magic to create the terms and the underlying
# clingo.Symbol object.
# ------------------------------------------------------------------------------

class NonLogicalSymbol(object, metaclass=_NonLogicalSymbolMeta):
    """Encapsulates an ASP predicate or complex term in an easy to access object.

    This is the heart of the ORM model for defining the mapping of a complex
    term or predicate to a Python object. ``Predicate`` and ``ComplexTerm`` are
    actually aliases for NonLogicalSymbol.

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
         - named parameters corresponding to the term names.

    """

    #--------------------------------------------------------------------------
    #
    #--------------------------------------------------------------------------
    def __init__(self):
        raise NotImplementedError(("Class {} can only be instantiated through a "
                                   "sub-class").format(self.__name__))


    #--------------------------------------------------------------------------
    # Properties and functions for NonLogicalSymbol
    #--------------------------------------------------------------------------

    # Get the underlying clingo.Symbol object
    @property
    def raw(self):
        """Returns the underlying clingo.Symbol object"""
        return self._raw
#        return self._generate_raw()

    @_classproperty
    def Field(cls):
        """A RawField sub-class corresponding to a Field for this class."""
        return cls._fieldcontainer.defn

    # Recompute the clingo.Symbol object from the stored term
    def _generate_raw(self):
        pred_args = []
        for field in self.meta:
            pred_args.append(field.defn.pytocl(self._term_values[field.name]))
        # Create the clingo.Symbol object
        return clingo.Function(self.meta.name, pred_args)

    # Clone the object with some differences
    def clone(self, **kwargs):
        """Clone the object with some differences.

        For any term name that is not one of the parameter keywords the clone
        keeps the same value. But for any term listed in the parameter keywords
        replace with specified new value.
        """

        # Sanity check
        clonekeys = set(kwargs.keys())
        objkeys = set(self.meta.keys())
        diffkeys = clonekeys - objkeys
        if diffkeys:
            raise ValueError("Unknown term names: {}".format(diffkeys))

        # Get the arguments for the new object
        cloneargs = {}
        for field in self.meta:
            if field.name in kwargs: cloneargs[field.name] = kwargs[field.name]
            else:
                cloneargs[field.name] = kwargs[field.name] = self._term_values[field.name]

        # Create the new object
        return type(self)(**cloneargs)

    #--------------------------------------------------------------------------
    # Class methods and properties
    #--------------------------------------------------------------------------

    # Get the metadata for the NonLogicalSymbol definition
    @_classproperty
    def meta(cls):
        """The meta data (definitional information) for the Predicate/Complex-term"""
        return cls._meta

    # Returns whether or not a clingo.Symbol object can unify with this
    # NonLogicalSymbol
    @classmethod
    def _unifies(cls, raw):
        if raw.type != clingo.SymbolType.Function: return False

        if raw.name != cls.meta.name: return False
        if len(raw.arguments) != len(cls.meta): return False

        for idx, field in enumerate(cls.meta):
            term = raw.arguments[idx]
            if not field.defn.unifies(raw.arguments[idx]): return False
        return True

    # Factory that returns a unified NonLogicalSymbol object
    @classmethod
    def _unify(cls, raw):
        return cls(raw=raw)

    #--------------------------------------------------------------------------
    # Overloaded index operator to access the values and len operator
    #--------------------------------------------------------------------------
    def __getitem__(self, idx):
        """Allows for index based access to term elements."""
        return self.meta[idx].__get__(self)

    def __bool__(self):
        '''Behaves like a tuple: returns False if the predicate/complex-term has no elements'''
        return len(self.meta) > 0

    def __len__(self):
        '''Returns the number of fields in the object'''
        return len(self.meta)

    #--------------------------------------------------------------------------
    # Overloaded operators
    #--------------------------------------------------------------------------
    def __eq__(self, other):
        """Overloaded boolean operator."""
        if not isinstance(other, self.__class__): return NotImplemented
        return self.raw == other.raw

    def __ne__(self, other):
        """Overloaded boolean operator."""
        result = self.__eq__(other)
        if result is NotImplemented: return NotImplemented
        return not result

    def __lt__(self, other):
        """Overloaded boolean operator."""
        if not isinstance(other, self.__class__): return NotImplemented

        # compare each field in order
        for idx in range(0,len(self._meta)):
            selfv = self[idx]
            otherv = other[idx]
            if selfv == otherv: continue
            return selfv < otherv
        return False

    def __ge__(self, other):
        """Overloaded boolean operator."""
        result = self.__lt__(other)
        if result is NotImplemented: return NotImplemented
        return not result

    def __gt__(self, other):
        """Overloaded boolean operator."""
        if not isinstance(other, self.__class__): return NotImplemented

        # compare each field in order
        for idx in range(0,len(self._meta)):
            selfv = self[idx]
            otherv = other[idx]
            if selfv == otherv: continue
            return selfv > otherv
        return False

    def __le__(self, other):
        """Overloaded boolean operator."""
        result = self.__gt__(other)
        if result is NotImplemented: return NotImplemented
        return not result

    def __hash__(self):
        return self.raw.__hash__()

    def __str__(self):
        """Returns the NonLogicalSymbol as the string representation of the raw
        clingo.Symbol.
        """
        return str(self.raw)

    def __repr__(self):
        return self.__str__()

#------------------------------------------------------------------------------
# Predicate and ComplexTerm are simply aliases for NonLogicalSymbol.
#------------------------------------------------------------------------------

Predicate=NonLogicalSymbol
ComplexTerm=NonLogicalSymbol

#------------------------------------------------------------------------------
#------------------------------------------------------------------------------

#------------------------------------------------------------------------------
# Generate facts from an input array of Symbols.  The unifiers argument is
# contains the names of predicate classes to unify against (order matters) and
# symbols contains the list of raw clingo.Symbol objects.
# ------------------------------------------------------------------------------

def unify(unifiers, symbols):
    '''Unify a collection of symbols against a list of predicate types.

    Symbols are tested against each unifier until a match is found. Since it is
    possible to define multiple predicate types that can unify with the same
    symbol, the order the unifiers differently can produce different results.

    Args:
      unifiers: a list of predicate classes to unify against
      symbols: the symbols to unify

    '''
    def unify_single(cls, r):
        try:
            return cls._unify(r)
        except ValueError:
            return None

    # To make things a little more efficient use the name/arity signature as a
    # filter. However, Python doesn't have a built in multidict and I don't want
    # to add an extra dependency - so this is a bit more complex than it needs
    # to be.
    sigs = [((cls.meta.name, len(cls.meta)),cls) for cls in unifiers]
    types = {}
    for sig,cls in sigs:
        if sig not in types: types[sig] = [cls]
        else: types[sig].append(cls)

    facts = []
    for raw in symbols:
        classes = types.get((raw.name, len(raw.arguments)))
        if not classes: continue
        for cls in classes:
            f = unify_single(cls,raw)
            if f:
                facts.append(f)
                break
    return facts


#------------------------------------------------------------------------------
#------------------------------------------------------------------------------

#------------------------------------------------------------------------------
# Fact comparator: is a function that determines if a fact (i.e., predicate
# instance) satisfies some property or condition. Any function that takes a
# single fact as a argument and returns a bool is a fact comparator. However, we
# define a few special types.
# ------------------------------------------------------------------------------

# A helper function to return a simplified version of a fact comparator
def _simplify_fact_comparator(comparator):
    try:
        return comparator.simplified()
    except:
        if isinstance(comparator, bool):
            return StaticComparator(comparator)
        return comparator

#------------------------------------------------------------------------------
# Placeholder allows for variable substituion of a query. Placeholder is
# an abstract class that exposes no API other than its existence.
# ------------------------------------------------------------------------------
class Placeholder(abc.ABC):
    """An abstract class for defining parameterised queries.

    Currently, Clorm supports 4 placeholders: ph1\_, ph2\_, ph3\_, ph4\_. These
    correspond to the positional arguments of the query execute function call.

    """
    pass

class _NamedPlaceholder(Placeholder):
    def __init__(self, name, default=None):
        self._name = str(name)
        self._default = default
        self._value = None
    @property
    def name(self):
        return self._name
    @property
    def default(self):
        return self._default
    def __str__(self):
        tmpstr = "" if not self._default else ",{}"
        return "ph_({}{})".format(self._name, tmpstr)

class _PositionalPlaceholder(Placeholder):
    def __init__(self, posn):
        self._posn = posn
        self._value = None
    @property
    def posn(self):
        return self._posn
    def reset(self):
        self._value = None
    def __str__(self):
        return "ph{}_".format(self._posn+1)

def ph_(value,default=None):
    try:
        idx = int(value)
    except ValueError:
        return _NamedPlaceholder(value,default)
    if default is not None:
        raise ValueError("Positional placeholders don't support default values")
    idx -= 1
    if idx < 0:
        raise ValueError("Index {} is not a positional argument".format(idx+1))
    return _PositionalPlaceholder(idx)

ph1_ = _PositionalPlaceholder(0)
ph2_ = _PositionalPlaceholder(1)
ph3_ = _PositionalPlaceholder(2)
ph4_ = _PositionalPlaceholder(3)

#------------------------------------------------------------------------------
# A Comparator is a boolean functor that takes a fact instance and returns
# whether it satisfies some condition.
# ------------------------------------------------------------------------------

class Comparator(abc.ABC):

    @abc.abstractmethod
    def __call__(self,fact, *args, **kwargs):
        pass

#------------------------------------------------------------------------------
# A Fact comparator functor that returns a static value
#------------------------------------------------------------------------------

class StaticComparator(Comparator):
    def __init__(self, value):
        self._value=bool(value)
    def __call__(self,fact, *args, **kwargs):
        return self._value
    def simpified(self):
        return self
    def placeholders(self): return []
    @property
    def value(self):
        return self._value

#------------------------------------------------------------------------------
# A fact comparator functor that tests whether a fact satisfies a comparision
# with the value of some predicate's term.
#
# Note: instances of FieldQueryComparator are constructed by calling the comparison
# operator for Field objects.
# ------------------------------------------------------------------------------
class FieldQueryComparator(Comparator):
    def __init__(self, compop, arg1, arg2):
        self._compop = compop
        self._arg1 = arg1
        self._arg2 = arg2
        self._static = False

        if not isinstance(arg1, _FieldPathEval):
            raise TypeError(("Internal error: argument 1 is not "
                             "a _FieldPathEval {}").format(arg1))
        # Comparison is trivial if:
        # 1) the objects are identical then it is a trivial comparison and
        # equivalent to checking if the operator satisfies a simple identity (eg., 1)
        # 2) neither argument is a Field
        if arg1 is arg2:
            self._static = True
            self._value = compop(1,1)
        elif isinstance(arg2, _FieldPathEval) and arg1.spec == arg2.spec:
            self._static = True
            self._value = compop(1,1)

    def __call__(self, fact, *args, **kwargs):
        if self._static: return self._value

        # Get the value of an argument (resolving placeholder)
        def getargval(arg):
            if isinstance(arg, _FieldPathEval): return arg(fact)
            elif isinstance(arg, _PositionalPlaceholder): return args[arg.posn]
            elif isinstance(arg, _NamedPlaceholder): return kwargs[arg.name]
            else: return arg

        # Get the values of the two arguments and then calculate the operator
        v1 = self._arg1(fact)
        v2 = getargval(self._arg2)

        # As much as possible check that the types should match - ie if the
        # first value is a complex term type then the second value should also
        # be of the same type. However, if the first value is a complex term and
        # the second is a tuple then we can try to convert the tuple into
        # complex term object of the first type.
        tryconv = False
        if type(v1) != type(v2):
            if isinstance(v1, NonLogicalSymbol):
                tryconv = True
                if isinstance(v2, NonLogicalSymbol) and v1.meta.name != v2.meta.name:
                    raise TypeError(("Incompatabile type comparison of "
                                     "{} and {}").format(v1,v2))
        if tryconv:
            try:
                v2 = type(v1)(*v2)
            except:
                raise TypeError(("Incompatabile type comparison of "
                                 "{} and {}").format(v1,v2))
        # Return the result of comparing the two values
        return self._compop(v1,v2)

    def simplified(self):
        if self._static: return StaticComparator(self._value)
        return self

    def placeholders(self):
        if isinstance(self._arg2, Placeholder): return [self._arg2]
        return []

    def indexable(self):
        if self._static: return None
        if isinstance(self._arg2, _FieldPathEval): return None
        return (self._arg1, self._compop, self._arg2)

    def __str__(self):
        if self._compop == operator.eq: opstr = "=="
        elif self._compop == operator.ne: opstr = "!="
        elif self._compop == operator.lt: opstr = "<"
        elif self._compop == operator.le: opstr = "<="
        elif self._compop == operator.gt: opstr = ">"
        elif self._compop == operator.et: opstr = ">="
        else: opstr = "<unknown>"

        return "{} {} {}".format(self._arg1, opstr, self._arg2)

#------------------------------------------------------------------------------
# A fact comparator that is a boolean operator over other Fact comparators
# ------------------------------------------------------------------------------

class BoolComparator(Comparator):
    def __init__(self, boolop, *args):
        if boolop not in [operator.not_, operator.or_, operator.and_]:
            raise TypeError("non-boolean operator")
        if boolop == operator.not_ and len(args) != 1:
            raise IndexError("'not' operator expects exactly one argument")
        elif boolop != operator.not_ and len(args) <= 1:
            raise IndexError("bool operator expects more than one argument")

        self._boolop=boolop
        self._args = args

    def __call__(self, fact, *args, **kwargs):
        if self._boolop == operator.not_:
            return operator.not_(self._args[0](fact,*args,**kwargs))
        elif self._boolop == operator.and_:
            for a in self._args:
                if not a(fact,*args,**kwargs): return False
            return True
        elif self._boolop == operator.or_:
            for a in self._args:
                if a(fact,*args,**kwargs): return True
            return False
        raise ValueError("unsupported operator: {}".format(self._boolop))

    def simplified(self):
        newargs=[]
        # Try and simplify each argument
        for arg in self._args:
            sarg = _simplify_fact_comparator(arg)
            if isinstance(sarg, StaticComparator):
                if self._boolop == operator.not_: return StaticComparator(not sarg.value)
                if self._boolop == operator.and_ and not sarg.value: sarg
                if self._boolop == operator.or_ and sarg.value: sarg
            else:
                newargs.append(sarg)
        # Now see if we can simplify the combination of the arguments
        if not newargs:
            if self._boolop == operator.and_: return StaticComparator(True)
            if self._boolop == operator.or_: return StaticComparator(False)
        if self._boolop != operator.not_ and len(newargs) == 1:
            return newargs[0]
        # If we get here there then there is a real boolean comparison
        return BoolComparator(self._boolop, *newargs)

    def placeholders(self):
        tmp = []
        for a in self._args:
            if isinstance(a, Comparator): tmp.extend(a.placeholders())
        return tmp

    @property
    def boolop(self): return self._boolop

    @property
    def args(self): return self._args

# ------------------------------------------------------------------------------
# Functions to build BoolComparator instances
# ------------------------------------------------------------------------------

def not_(*conditions):
    return BoolComparator(operator.not_,*conditions)
def and_(*conditions):
    return BoolComparator(operator.and_,*conditions)
def or_(*conditions):
    return BoolComparator(operator.or_,*conditions)

#------------------------------------------------------------------------------
# _FactIndex indexes facts by a given field
#------------------------------------------------------------------------------

# Support function to make sure the object is a canonical FieldPath. If it's a
# FieldPathBuilder object then return the corresponding FieldPath.
def _to_field_path(obj):
    if isinstance(obj, FieldPathBuilder):
        return obj.meta.field_path()
    if not isinstance(obj, FieldPath):
        raise TypeError(("{} is not a FieldPathBuilder or FieldPath "
                         "instance").format(obj))
    return obj.canonical()


class _FactIndex(object):
    def __init__(self, fpspec):
        self._fpspec = _to_field_path(fpspec)
        self._predicate = self._fpspec.predicate
        self._fpgetter = _FieldPathEval(self._fpspec)
        self._keylist = []
        self._key2values = {}

    def field_path(self):
        return self._fpspec

    def add(self, fact):
        if not isinstance(fact, self._predicate):
            raise TypeError("{} is not a {}".format(fact, self._predicate))
        key = self._fpgetter(fact)

        # Index the fact by the key
        if key not in self._key2values: self._key2values[key] = set()
        self._key2values[key].add(fact)

        # Maintain the sorted list of keys
        posn = bisect.bisect_left(self._keylist, key)
        if len(self._keylist) > posn and self._keylist[posn] == key: return
        bisect.insort_left(self._keylist, key)

    def discard(self, fact):
        self.remove(fact, False)

    def remove(self, fact, raise_on_missing=True):
        if not isinstance(fact, self._predicate):
            raise TypeError("{} is not a {}".format(fact, self._predicate))
        key = self._fpgetter(fact)

        # Remove the value
        if key not in self._key2values:
            if raise_on_missing:
                raise KeyError("{} is not in the FactIndex".format(fact))
            return
        values = self._key2values[key]
        if raise_on_missing: values.remove(fact)
        else: values.discard(fact)

        # If still have values then we're done
        if values: return

        # remove the key
        del self._key2values[key]
        posn = bisect.bisect_left(self._keylist, key)
        del self._keylist[posn]

    def clear(self):
        self._keylist = []
        self._key2values = {}

    @property
    def keys(self): return self._keylist

    #--------------------------------------------------------------------------
    # Internal functions to get keys matching some boolean operator
    #--------------------------------------------------------------------------

    def _keys_eq(self, key):
        if key in self._key2values: return [key]
        return []

    def _keys_ne(self, key):
        posn1 = bisect.bisect_left(self._keylist, key)
        if posn1: left =  self._keylist[:posn1]
        else: left = []
        posn2 = bisect.bisect_right(self._keylist, key)
        if posn2: right = self._keylist[posn2:]
        else: right = []
        return left + right

    def _keys_lt(self, key):
        posn = bisect.bisect_left(self._keylist, key)
        if posn: return self._keylist[:posn]
        return []

    def _keys_le(self, key):
        posn = bisect.bisect_right(self._keylist, key)
        if posn: return self._keylist[:posn]
        return []

    def _keys_gt(self, key):
        posn = bisect.bisect_right(self._keylist, key)
        if posn: return self._keylist[posn:]
        return []

    def _keys_ge(self, key):
        posn = bisect.bisect_left(self._keylist, key)
        if posn: return self._keylist[posn:]
        return []

    #--------------------------------------------------------------------------
    # Find elements based on boolean match to a key
    #--------------------------------------------------------------------------
    def find(self, op, key):
        keys = []
        if op == operator.eq: keys = self._keys_eq(key)
        elif op == operator.ne: keys = self._keys_ne(key)
        elif op == operator.lt: keys = self._keys_lt(key)
        elif op == operator.le: keys = self._keys_le(key)
        elif op == operator.gt: keys = self._keys_gt(key)
        elif op == operator.ge: keys = self._keys_ge(key)
        else: raise ValueError("unsupported operator {}".format(op))

        sets = [ self._key2values[k] for k in keys ]
        if not sets: return set()
        return set.union(*sets)

#------------------------------------------------------------------------------
# Select is an interface query over a FactBase.
# ------------------------------------------------------------------------------

class Select(abc.ABC):

    @abc.abstractmethod
    def where(self, *expressions):
        """Set the select statement's where clause.

        The where clause consists of a set of comparison expressions. A
        comparison expression is simply a test functor that takes a predicate
        instance and returns whether or not that instance satisfies some
        requirement. Hence any lambda or function with this signature can be
        passed.

        Such test functors can also be generated using a more natural syntax,
        simply by making a boolean comparison between a field and a some other
        object. This is acheived by overloading the field boolean comparison
        operators to return a functor.

        The second parameter can point to an arbitrary value or a special
        placeholder value that issubstituted when the query is actually
        executed. These placeholders are named ``ph1_``, ``ph2_``, ``ph3_``, and
        ``ph4_`` and correspond to the 1st to 4th arguments of the ``get``,
        ``get_unique`` or ``count`` function call.

        Args:
          expressions: one or more comparison expressions.

        """
        pass

    @abc.abstractmethod
    def order_by(self, *fieldorder):
        """Provide an ordering over the results."""
        pass

    @abc.abstractmethod
    def get(self, *args, **kwargs):
        """Return all matching entries."""
        pass

    @abc.abstractmethod
    def get_unique(self, *args, **kwargs):
        """Return the single matching entry. Raises ValueError otherwise."""
        pass

    @abc.abstractmethod
    def count(self, *args, **kwargs):
        """Return the number of matching entries."""
        pass

#------------------------------------------------------------------------------
# Delete is an interface to perform a query delete from a FactBase.
# ------------------------------------------------------------------------------

class Delete(abc.ABC):

    @abc.abstractmethod
    def where(self, *expressions):
        pass

    @abc.abstractmethod
    def execute(self, *args, **kwargs):
        pass

#------------------------------------------------------------------------------
# A selection over a _FactMap
#------------------------------------------------------------------------------

class _Select(Select):

    def __init__(self, factmap):
        self._factmap = factmap
        self._index_priority = { f:p for (p,f) in enumerate(factmap.indexes) }
        self._where = None
        self._indexable = None
        self._key = None

    def where(self, *expressions):
        if self._where:
            raise ValueError("cannot specify 'where' multiple times")
        if not expressions:
            raise ValueError("empty 'where' expression")
        elif len(expressions) == 1:
            self._where = _simplify_fact_comparator(expressions[0])
        else:
            self._where = _simplify_fact_comparator(and_(*expressions))

        self._indexable = self._primary_search(self._where)
        return self

    @property
    def has_where(self):
        return bool(self._where)

    def order_by(self, *expressions):
        if self._key:
            raise ValueError("cannot specify 'order_by' multiple times")
        if not expressions:
            raise ValueError("empty 'order_by' expression")
        field_orders = []
        for exp in expressions:
            if isinstance(exp, FieldOrderBy): field_orders.append(exp)
            elif isinstance(exp, FieldPathBuilder): field_orders.append(exp.meta.asc())
            else: raise ValueError("Invalid field order expression: {}".format(exp))

        # Create a comparator function
        def mycmp(a, b):
            for ford in field_orders:
                value = ford.compare(a,b)
                if value == 0: continue
                return value
            return 0

        self._key = functools.cmp_to_key(mycmp)
        return self

    def _primary_search(self, where):
        def validate_indexable(indexable):
            if not indexable: return None
            if indexable[0].spec not in self._index_priority: return None
            return indexable

        if isinstance(where, FieldQueryComparator):
            return validate_indexable(where.indexable())
        indexable = None
        if isinstance(where, BoolComparator) and where.boolop == operator.and_:
            for arg in where.args:
                tmp = self._primary_search(arg)
                if tmp:
                    if not indexable: indexable = tmp
                    elif self._index_priority[tmp[0].spec] < \
                         self._index_priority[indexable[0].spec]:
                        indexable = tmp
        return indexable

#    @property
    def _debug(self):
        return self._indexable

    # Support function to check that arguments match placeholders and assign any
    # default values for named placeholders.
    def _resolve_arguments(self, *args, **kwargs):
        if not self._where: return kwargs
        if not isinstance(self._where, Comparator): return kwargs
        new_kwargs = {}
        placeholders = self._where.placeholders()
        for ph in placeholders:
            if isinstance(ph, _PositionalPlaceholder):
                if ph.posn < len(args): continue
                raise TypeError(("missing argument in {} for placeholder "
                                 "{}").format(args, ph))
            elif isinstance(ph, _NamedPlaceholder):
                if ph.name in kwargs: continue
                elif ph.default is not None:
                    new_kwargs[ph.name] = ph.default
                    continue
                raise TypeError(("missing argument in {} for named "
                                 "placeholder with no default "
                                 "{}").format(kwargs, args))
            raise TypeError("unknown placeholder {} ({})".format(ph, type(ph)))

        # Add any new values
        if not new_kwargs: return kwargs
        new_kwargs.update(kwargs)
        return new_kwargs

    # Function to execute the select statement
    def get(self, *args, **kwargs):

        nkwargs = self._resolve_arguments(*args, **kwargs)

        # Function to get a value - resolving placeholder if necessary
        def get_value(arg):
            if isinstance(arg, _PositionalPlaceholder): return args[arg.posn]
            elif isinstance(arg, _NamedPlaceholder): return nkwargs[arg.name]
            else: return arg

        # If there is no index test all instances else use the index
        result = []
        if not self._indexable:
            for f in self._factmap.facts():
                if not self._where: result.append(f)
                elif self._where and self._where(f,*args,**nkwargs): result.append(f)
        else:
            findex = self._factmap.get_factindex(self._indexable[0])
            value = get_value(self._indexable[2])
            fp = findex.field_path()
            cmplx = fp.defn.complex
            if cmplx and isinstance(value, tuple): value = cmplx(*value)
            for f in findex.find(self._indexable[1], value):
                if self._where(f,*args,**nkwargs): result.append(f)

        # Return the results - sorted if necessary
        if self._key: result.sort(key=self._key)
        return result

    def get_unique(self, *args, **kwargs):
        count=0
        fact=None
        for f in self.get(*args, **kwargs):
            fact=f
            count += 1
            if count > 1:
                raise ValueError("Multiple facts found - exactly one expected")
        if count == 0:
            raise ValueError("No facts found - exactly one expected")
        return fact

    def count(self, *args, **kwargs):
        return len(self.get(*args, **kwargs))

#------------------------------------------------------------------------------
# A deletion over a _FactMap
# - a stupid implementation that iterates over all facts and indexes
#------------------------------------------------------------------------------

class _Delete(Delete):

    def __init__(self, factmap):
        self._factmap = factmap
        self._select = _Select(factmap)

    def where(self, *expressions):
        self._select.where(*expressions)
        return self

    def execute(self, *args, **kwargs):
        # If there is no where clause then delete everything
        if not self._select.has_where:
            num_deleted = len(self._factmap.facts())
            self._factmap.clear()
            return num_deleted

        # Gather all the facts to delete and remove them
        to_delete = [ f for f in self._select.get(*args, **kwargs) ]
        for fact in to_delete: self._factmap.remove(fact)
        return len(to_delete)

#------------------------------------------------------------------------------
# A map for facts of the same type - Indexes can be built to allow for fast
# lookups based on a term value. The order that the terms are specified in the
# index matters as it determines the priority of the index.
# ------------------------------------------------------------------------------

class _FactMap(object):
    def __init__(self, ptype, index=[]):
        self._ptype = ptype
        self._allfacts = set()
        self._findexes = None
        if not issubclass(ptype, Predicate):
            raise TypeError("{} is not a subclass of Predicate".format(ptype))
        if index:
            clean = [ _to_field_path(f) for f in index ]
            self._findexes = collections.OrderedDict( (f, _FactIndex(f)) for f in clean )
            prts = set([f.predicate for f in clean])
            if len(prts) != 1 or prts != set([ptype]):
                raise TypeError("Fields in {} do not belong to {}".format(index,prts))

    def add(self, fact):
        self._allfacts.add(fact)
        if self._findexes:
            for findex in self._findexes.values(): findex.add(fact)

    def discard(self, fact):
        self.remove(fact, False)

    def remove(self, fact, raise_on_missing=True):
        if raise_on_missing: self._allfacts.remove(fact)
        else: self._allfacts.discard(fact)
        if self._findexes:
            for findex in self._findexes.values(): findex.remove(fact,raise_on_missing)

    @property
    def indexes(self):
        if not self._findexes: return []
        return [ f for f, vs in self._findexes.items() ]

    def get_factindex(self, field):
        return self._findexes[field.spec]

    def facts(self):
        return self._allfacts

    def clear(self):
        self._allfacts.clear()
        if self._findexes:
            for f, findex in self._findexes.items(): findex.clear()

    def select(self):
        return _Select(self)

    def delete(self):
        return _Delete(self)

    def asp_str(self):
        out = io.StringIO()
        for f in self._allfacts:
            print("{}.".format(f), file=out)
        data = out.getvalue()
        out.close()
        return data

    def __str__(self):
        return self.asp_str()

    #--------------------------------------------------------------------------
    # Special functions to support container operations
    #--------------------------------------------------------------------------

    def __contains__(self, fact):
        if not isinstance(fact, self._ptype): return False
        return fact in self._allfacts

    def __bool__(self):
        return bool(self._allfacts)

    def __len__(self):
        return len(self._allfacts)

    def __iter__(self):
        return iter(self._allfacts)

#------------------------------------------------------------------------------
# FactBaseBuilder offers a decorator interface for gathering predicate and index
# definitions to be used in defining a FactBase subclass.
# ------------------------------------------------------------------------------
class FactBaseBuilder(object):
    def __init__(self, predicates=[], indexes=[], suppress_auto_index=False):
        self._predicates = []
        self._indexes = []
        self._predset = set()
        self._indset = set()
        self._suppress_auto_index = suppress_auto_index
        for pred in predicates: self._register_predicate(pred)
        for f in indexes: self._register_index(f)

    def _register_predicate(self, cls):
        if cls in self._predset: return    # ignore if already registered
        if not issubclass(cls, Predicate):
            raise TypeError("{} is not a Predicate sub-class".format(cls))
        self._predset.add(cls)
        self._predicates.append(cls)
        if self._suppress_auto_index: return

        # DPR Fixup - add sub-field indexes
        # Register the fields that have the index flag set
        for field in cls.meta:
            # Hmm. can't remember why I might get an attributeerror?
            with contextlib.suppress(AttributeError):
                if field.defn.index: self._register_index(field.__get__(None))

    def _register_index(self, fp):
        fp = _to_field_path(fp)
        if fp in self._indset: return    # ignore if already registered
        if isinstance(fp, FieldPath) and fp.predicate in self.predicates:
            self._indset.add(fp)
            self._indexes.append(fp)
        else:
            raise TypeError("{} is not a predicate field for one of {}".format(
                fp, [ p.__name__ for p in self.predicates ]))

    def register(self, cls):
        self._register_predicate(cls)
        return cls

    def new(self, facts=None, symbols=None, delayed_init=False, raise_on_empty=False):
        if not symbols and (delayed_init or raise_on_empty):
            raise ValueError("'delayed_init' and 'raise_on_empty' only valid for symbols")
        if symbols and facts:
            raise ValueError("'symbols' and 'facts' options are mutually exclusive")

        def _populate():
            facts=unify(self.predicates, symbols)
            if not facts and raise_on_empty:
                raise ValueError("FactBase creation: failed to unify any symbols")
            return facts

        if delayed_init:
            return FactBase(facts=_populate, indexes=self._indexes)
        if symbols:
            return FactBase(facts=_populate(), indexes=self._indexes)
        else:
            return FactBase(facts=facts, indexes=self._indexes)

    @property
    def predicates(self): return self._predicates
    @property
    def indexes(self): return self._indexes

#------------------------------------------------------------------------------
# A FactBase consisting of facts of different types
#------------------------------------------------------------------------------

class FactBase(object):
    """A fact base is a container for facts that must be subclassed.

    ``FactBase`` can be thought of as a minimalist database. It stores facts for
    ``Predicate`` types (where a predicate type loosely corresponding to a
    *table* in a database) and allows for certain fields to be indexed in order
    to perform more efficient queries.

    Args:
      facts([Predicate]|callable): a list of facts (predicate instances), or a
         functor that generates. If a functor is passed then the factbase
         performs a delayed initialisation.
      indexes(Field): a list of fields that are to be indexed.

    """

    #--------------------------------------------------------------------------
    # Internal member functions
    #--------------------------------------------------------------------------

    # A special purpose initialiser so that we can do delayed initialisation
    def _init(self, facts=None, indexes=[]):

        # flag that initialisation has taken place
        self._delayed_init = None

        # If it is delayed initialisation then get the facts
        if facts and callable(facts): facts = facts()

        # Create _FactMaps for the predicate types with indexed terms
        grouped = {}

        clean = [ _to_field_path(f) for f in indexes ]
        for fp in clean:
            if fp.predicate not in grouped: grouped[fp.predicate] = []
            grouped[fp.predicate].append(fp)
        self._factmaps = { pt : _FactMap(pt, fps) for pt, fps in grouped.items() }

        if facts is None: return
        self._add(facts)

    #--------------------------------------------------------------------------
    #
    #--------------------------------------------------------------------------

    def _add(self, arg):
        if isinstance(arg, Predicate): return self._add_fact(arg)
        for f in arg: self._add_fact(f)

    # Helper for _add
    def _add_fact(self, fact):
        ptype = type(fact)
        if not issubclass(ptype,Predicate):
            raise TypeError(("type of object {} is not a Predicate "
                             "subclass").format(fact))
        if ptype not in self._factmaps:
            self._factmaps[ptype] = _FactMap(ptype)
        self._factmaps[ptype].add(fact)

    def _remove(self, fact, raise_on_missing):
        ptype = type(fact)
        if not isinstance(arg, Predicate) or ptype not in self._factmaps:
            raise KeyError("{} not in factbase".format(arg))

        return self._factmaps[ptype].delete()

    #--------------------------------------------------------------------------
    # Special functions to support container operations
    #--------------------------------------------------------------------------

    def __contains__(self, fact):
        # Always check if we have delayed initialisation
        if self._delayed_init: self._delayed_init()

        if not isinstance(fact,Predicate): return False
        ptype = type(fact)
        if ptype not in self._factmaps: return False
        return fact in self._factmaps[ptype].facts()

    def __bool__(self):
        # Always check if we have delayed initialisation
        if self._delayed_init: self._delayed_init()

        for fm in self._factmaps.values():
            if fm: return True
        return False

    def __len__(self):
        # Always check if we have delayed initialisation
        if self._delayed_init: self._delayed_init()

        return sum([len(fm) for fm in self._factmaps.values()])

    def __iter__(self):
        # Always check if we have delayed initialisation
        if self._delayed_init: self._delayed_init()

        for fm in self._factmaps.values():
            for f in fm: yield f

    def __eq__(self, other):
        """Overloaded boolean operator."""
        if not isinstance(other, self.__class__): return NotImplemented

        # Always check if we have delayed initialisation
        if self._delayed_init: self._delayed_init()

        if len(self) != len(other): return False
        for f in self:
            if f not in other: return False
        return True

    def __ne__(self, other):
        """Overloaded boolean operator."""
        result = self.__eq__(other)
        if result is NotImplemented: return NotImplemented
        return not result

    #--------------------------------------------------------------------------
    # Initiliser
    #--------------------------------------------------------------------------
    def __init__(self, facts=None, indexes=[]):
        self._delayed_init=None
        if callable(facts):
            def delayed_init():
                self._init(facts, indexes)
            self._delayed_init=delayed_init
        else:
            self._init(facts, indexes)


    #--------------------------------------------------------------------------
    # Set member functions
    #--------------------------------------------------------------------------
    def add(self, arg):
        # Always check if we have delayed initialisation
        if self._delayed_init: self._delayed_init()
        return self._add(arg)

    def remove(self, arg):
        # Always check if we have delayed initialisation
        if self._delayed_init: self._delayed_init()
        return self._remove(arg, raise_on_missing=True)

    def discard(self, arg):
        # Always check if we have delayed initialisation
        if self._delayed_init: self._delayed_init()
        return self._remove(arg, raise_on_missing=False)

    def clear(self):
        """Clear the fact base of all facts."""

        # Always check if we have delayed initialisation
        if self._delayed_init: self._delayed_init()
        for pt, fm in self._factmaps.items(): fm.clear()

    #--------------------------------------------------------------------------
    # Special FactBase member functions
    #--------------------------------------------------------------------------
    def select(self, ptype):
        """Create a Select query for a predicate type."""

        # Always check if we have delayed initialisation
        if self._delayed_init: self._delayed_init()

        if ptype not in self._factmaps:
            self._factmaps[ptype] = _FactMap(ptype)
        return self._factmaps[ptype].select()

    def delete(self, ptype):
        """Create a Select query for a predicate type."""

        # Always check if we have delayed initialisation
        if self._delayed_init: self._delayed_init()

        if ptype not in self._factmaps:
            self._factmaps[ptype] = _FactMap(ptype)
        return self._factmaps[ptype].delete()

    @property
    def predicates(self):
        """Return the list of predicate types that this fact base contains."""
        # Always check if we have delayed initialisation
        if self._delayed_init: self._delayed_init()
        return [pt for pt, fm in self._factmaps.items() if fm.facts()]

    @property
    def indexes(self):
        if self._delayed_init: self._delayed_init()
        tmp = []
        for fm in self._factmaps.values():
            tmp.extend(fm.indexes)
        return tmp

    def facts(self):
        """Return all facts."""

        # Always check if we have delayed initialisation
        if self._delayed_init: self._delayed_init()
        fcts = []
        for fm in self._factmaps.values():
            fcts.extend(fm.facts())
        return fcts

    def asp_str(self):
        """Return a string representation of the fact base that is suitable for adding
        to an ASP program

        """

        # Always check if we have delayed initialisation
        if self._delayed_init: self._delayed_init()

        out = io.StringIO()
        for fm in self._factmaps.values():
            for f in fm:
                print("{}.".format(f), file=out)
        data = out.getvalue()
        out.close()
        return data

    def __str__(self):
        tmp = ", ".join([str(f) for f in self])
        return '{' + tmp + '}'

#------------------------------------------------------------------------------
#------------------------------------------------------------------------------


#------------------------------------------------------------------------------
# When calling Python functions from ASP you need to do some type
# conversions. The TypeCastSignature class can be used to generate a wrapper
# function that does the type conversion for you.
# ------------------------------------------------------------------------------

class TypeCastSignature(object):
    """Defines a signature for converting to/from Clingo data types.

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
        '''An input element must be a subclass of RawField (or an instance of a
           subclass) or a tuple corresponding to a subclass of RawField'''
        return inspect.isclass(se) and issubclass(se, RawField)

    @staticmethod
    def is_return_element(se):
        '''An output element must be a subclass of RawField or a singleton containing'''
        if isinstance(se, collections.Iterable):
            if len(se) != 1: return False
            return TypeCastSignature._is_input_element(se[0])
        return TypeCastSignature._is_input_element(se)

    def __init__(self, *sigs):
        def _validate_basic_sig(sig):
            if TypeCastSignature._is_input_element(sig): return True
            raise TypeError(("TypeCastSignature element {} must be a RawField "
                             "subclass".format(sig)))

        self._insigs = [ type(_get_field_defn(s)) for s in sigs[:-1]]
#        self._insigs = sigs[:-1]
        self._outsig = sigs[-1]

        # A tuple is a special case that we want to convert into a complex field
        if isinstance(self._outsig, tuple):
            self._outsig = type(_get_field_defn(self._outsig))
        elif isinstance(self._outsig, collections.Iterable):
            if len(self._outsig) != 1:
                raise TypeError("Return value list signature not a singleton")
            if isinstance(self._outsig[0], tuple):
                self._outsig[0] = type(_get_field_defn(self._outsig[0]))

        # Validate the signature
        for s in self._insigs: _validate_basic_sig(s)
        if isinstance(self._outsig, collections.Iterable):
            _validate_basic_sig(self._outsig[0])
        else:
            _validate_basic_sig(self._outsig)

    def _input(self, sig, arg):
        return sig.cltopy(arg)

    def _output(self, sig, arg):
        # Since signature already validated we can make assumptions
        if inspect.isclass(sig) and issubclass(sig, RawField):
            return sig.pytocl(arg)

        # Deal with a list
        if isinstance(sig, collections.Iterable) and isinstance(arg, collections.Iterable):
            return [ self._output(sig[0], v) for v in arg ]
        raise ValueError("Value {} does not match signature {}".format(arg, sig))


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

#------------------------------------------------------------------------------
# return and check that function has complete signature
# annotations. ignore_first is useful when dealing with member functions.
#------------------------------------------------------------------------------

def _get_annotations(fn, ignore_first=False):
    fsig = inspect.signature(fn)
    fsigparam = fsig.parameters
    annotations = [fsigparam[s].annotation for s in fsigparam]
    if not annotations and ignore_first:
        raise TypeError("Empty function signature - cannot ignore first element")
    annotations.append(fsig.return_annotation)
    if ignore_first: annotations.pop(0)
    if inspect.Signature.empty in annotations:
        raise TypeError("Failed to extract all annotations from {} ".format(fn))
    return annotations


#------------------------------------------------------------------------------
#------------------------------------------------------------------------------

def make_function_asp_callable(*args):
    '''A decorator for making a function callable from within an ASP program.

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

    '''
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
    '''A decorator for making a member function callable from within an ASP program.

    See ``make_function_asp_callable`` for details. The only difference is that
    the first element of the function is ignore as it is assumed to be the
    ``self`` or ``cls`` parameter.

    '''
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
# main
#------------------------------------------------------------------------------
if __name__ == "__main__":
    raise RuntimeError('Cannot run modules')
