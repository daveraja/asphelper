#!/usr/bin/env python

#------------------------------------------------------------------------------
# Instantiating a FactBase with lots of elements
#------------------------------------------------------------------------------

from clorm import Predicate, ComplexTerm, ConstantField, \
    IntegerField, StringField,FactBase
import clorm
from clingo import Function,Number

import time
import cProfile
import pstats
from pstats import SortKey

#------------------------------------------------------------------------------
#
#------------------------------------------------------------------------------

class Profiler(object):
    def __init__(self,msg):
        self._msg=msg
        self._calls=[]
        self._justified=0

    def __call__(self,msg,func,*args,**kwargs):
        self._justified=max(len(msg)+3, self._justified)
        starttime = time.process_time()
        res=func(*args,**kwargs)
        endtime = time.process_time()
        self._calls.append((msg,endtime-starttime))
        return res

    @property
    def justified(self): return self._justified

    def print_stats(self,justified=0):
        if justified < self._justified: justified = self._justified
        print("\n".ljust(justified+10,'='))
        print("{}".format(self._msg))
        if not self._calls:
            print(" -------- No functions profiled -----------\n")
            return
        total=0.0
        for msg,cputime in self._calls:
            print("{}: {:.3f}".format(msg.ljust(justified),cputime))
            total += cputime
        print("{}: {:.3f}".format("Total time".ljust(justified),total))


#------------------------------------------------------------------------------
# A data model
#------------------------------------------------------------------------------

class Customer(Predicate):
    cid=IntegerField
    name=StringField

class Sale(Predicate):
    sid=IntegerField
    cid=IntegerField
    item=StringField


#------------------------------------------------------------------------------
#
#------------------------------------------------------------------------------

def create_facts(num_customers,sales_per_customer):
    tmp=[]
    saleid=1
    for idx in range(1,num_customers+1):
        c = Customer(cid=idx,name="Customer {}".format(idx))
        tmp.append(c)
        for idx2 in range(1,sales_per_customer+1):
            sale=Sale(sid=saleid,cid=c.cid,item="Item {}".format(saleid))
            saleid += 1
            tmp.append(sale)
    return tmp

def make_fb(facts,indexed):
    if indexed:
        return FactBase(facts,indexes=[Customer.cid,Sale.cid])
    else:
        return FactBase(facts)

def run_count(qo):
    print("Query returned: {} records".format(qo.count()))

    
def run_print(qo):
    for name,sales in qo.output(Sale).group_by():
        print("Customer: {} => {}".format(name,
                                          len(sorted(list(sales),key=lambda s: s.sid ))))

def customer_sorted_query(fb):
    print("===========================================================")
    print("Index for Factbase with indexes: {}".format(fb.indexes))
    q=fb.select(Customer,Sale).join(Customer.cid == Sale.cid) \
                              .order_by(Customer.name)
    print("Query Plan\n{}".format(q.query_plan()))
    print("===========================================================")
    return q.run()

def all_sorted_query(fb):
    print("===========================================================")
    print("Index for Factbase with indexes: {}".format(fb.indexes))
    q=fb.select(Customer,Sale).join(Customer.cid == Sale.cid) \
                              .order_by(Customer.name,Sale.sid)
    print("Query Plan\n{}".format(q.query_plan()))
    print("===========================================================")
    return q.run()

g_facts=None

def run_fact_querying(nc,spc):
    global g_facts
    def go():
        global g_facts
        g_facts = create_facts(nc,spc)

    pr=Profiler("Timing for fact creation and querying")
    msg1 = "Intstantiating {} new fact instances".format(nc*spc)
    pr(msg1, go)
    fb1 = pr("Adding facts to non-indexed FactBase", lambda : make_fb(g_facts,False))
    fb2 = pr("Adding facts to indexed FactBase", lambda : make_fb(g_facts,True))
#    qo1 = pr("Query non-indexed FactBase", lambda : query(fb1))
#    qo2 = pr("Query indexed FactBase", lambda : customer_sorted_query(fb2))
    qo2 = pr("Query indexed FactBase", lambda : all_sorted_query(fb2))
#    c1 = pr("Counting non-indexed query", lambda : run_count(qo1))
#    c1 = pr("Counting indexed query", lambda : run_count(qo2))
#    qo2 = pr("Query indexed FactBase - second", lambda : query(fb2))
    c1 = pr("Printing indexed query", lambda : run_print(qo2))

    return pr


def main():
    print("\nProfiling Querying FactBase")

    # Profile the non-index and index fact bases
    pr1 = run_fact_querying(100,10000)
    justified=0
    pr1.print_stats(justified=justified)

#------------------------------------------------------------------------------
# main
#------------------------------------------------------------------------------
if __name__ == "__main__":
    main()

