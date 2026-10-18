"""Bounded numerical HGB JSON inference; no pickle, training, or implicit fallback."""
import json, math
from pathlib import Path
import numpy as np
from .experimental_features import FEATURES

class FrozenHGB:
    def __init__(self, payload):
        if payload.get('schema')!='tail-hgb-numeric-v1' or payload.get('feature_names')!=FEATURES:
            raise ValueError('experimental_model_schema_mismatch')
        if payload.get('model_id')!='study06-hgb33-fold2021':raise ValueError('unexpected_model_id')
        self.metadata={k:v for k,v in payload.items() if k!='trees'}
        self.baseline=float(payload['baseline'])
        if not math.isfinite(self.baseline):raise ValueError('nonfinite_baseline')
        trees=payload.get('trees')
        if not isinstance(trees,list) or len(trees)!=150:raise ValueError('invalid_tree_count')
        self.trees=[]
        for tree in trees:
            if not isinstance(tree,list) or not 1<=len(tree)<=13:raise ValueError('invalid_tree_size')
            visited=set()
            def visit(i):
                if type(i) is not int or not 0<=i<len(tree) or i in visited:raise ValueError('invalid_tree_graph')
                visited.add(i);n=tree[i]
                if set(n)!={'feature','threshold','left','right','leaf','value'} or type(n['leaf']) is not bool:raise ValueError('invalid_node_schema')
                if not all(type(n[k]) in (int,float) and math.isfinite(n[k]) for k in ('threshold','value')):raise ValueError('nonfinite_node')
                if not n['leaf']:
                    if type(n['feature']) is not int or not 0<=n['feature']<len(FEATURES):raise ValueError('invalid_feature')
                    visit(n['left']);visit(n['right'])
            visit(0)
            if len(visited)!=len(tree):raise ValueError('unreachable_tree_nodes')
            self.trees.append({k:np.asarray([n[k] for n in tree],dtype=bool if k=='leaf' else np.int32 if k in ('feature','left','right') else np.float64) for k in tree[0]})

    @classmethod
    def load(cls,path):
        path=Path(path)
        if path.stat().st_size>2_000_000:raise ValueError('model_too_large')
        def pairs(items):
            d={}
            for k,v in items:
                if k in d:raise ValueError('duplicate_json_key')
                d[k]=v
            return d
        return cls(json.loads(path.read_text(encoding='utf-8'),object_pairs_hook=pairs))

    def predict(self,values):
        x=np.asarray(values,dtype=np.float32)
        if x.ndim!=2 or x.shape[1]!=len(FEATURES) or not np.isfinite(x).all():raise ValueError('experimental_features_invalid')
        # Training matrices were stored as float32; preserve that rounding.
        x=x.astype(np.float64)
        result=np.full(len(x),self.baseline,dtype=np.float64)
        for tree in self.trees:
            active=np.arange(len(x));nodes=np.zeros(len(x),dtype=np.int32)
            while len(active):
                pos=nodes[active];leaf=tree['leaf'][pos]
                result[active[leaf]]+=tree['value'][pos[leaf]]
                active=active[~leaf];pos=pos[~leaf]
                if len(active):
                    left=x[active,tree['feature'][pos]]<=tree['threshold'][pos]
                    nodes[active]=np.where(left,tree['left'][pos],tree['right'][pos])
        return result
