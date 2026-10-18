"""Strict NumPy inference for official LightGBM numerical regression dumps.

No training or LightGBM runtime dependency. Scores are raw regression scores,
never probabilities. Supported numerical decisions follow LightGBM v4.7.0
``include/LightGBM/tree.h`` and ``meta.h`` (NumericalDecision/IsZero).
Unknown model semantics are rejected instead of silently approximated.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import math
from numbers import Integral, Real
from pathlib import Path

import numpy as np


ZERO_THRESHOLD = float(np.float32(1e-35))  # C++ const double = 1e-35f
MAX_MODEL_BYTES = 256 * 1024 * 1024
_ROOT_KEYS = {"name", "version", "num_class", "num_tree_per_iteration", "label_index",
              "max_feature_idx", "objective", "average_output", "feature_names",
              "monotone_constraints", "feature_infos", "feature_importances", "tree_info",
              "pandas_categorical"}
_TREE_KEYS = {"tree_index", "num_leaves", "num_cat", "shrinkage", "tree_structure"}
_SPLIT_KEYS = {"split_index", "split_feature", "split_gain", "threshold", "decision_type",
               "default_left", "missing_type", "internal_value", "internal_weight",
               "internal_count", "left_child", "right_child"}
_LEAF_KEYS = {"leaf_index", "leaf_value", "leaf_weight", "leaf_count"}


def _integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, Integral) or value < minimum:
        raise ValueError(f"invalid integer: {name}")
    return int(value)


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise ValueError(f"invalid finite number: {name}")
    return float(value)


def _names(values):
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError("feature names must be an ordered sequence")
    result = tuple(values)
    if not result or any(not isinstance(name, str) or not name for name in result):
        raise ValueError("feature names must be nonempty strings")
    if len(set(result)) != len(result):
        raise ValueError("duplicate feature names are not supported")
    return result


def _keys(value, allowed, label):
    if not isinstance(value, Mapping) or set(value) - allowed:
        raise ValueError(f"unknown or invalid {label} structure")


@dataclass(frozen=True)
class _Tree:
    feature: np.ndarray
    threshold: np.ndarray
    left: np.ndarray
    right: np.ndarray
    missing: np.ndarray
    default_left: np.ndarray
    leaf: np.ndarray


def _compile_tree(tree, expected_index, feature_count):
    _keys(tree, _TREE_KEYS, "tree")
    if _integer(tree.get("tree_index"), "tree_index") != expected_index:
        raise ValueError("trees must retain their official contiguous order")
    count = _integer(tree.get("num_leaves"), "num_leaves", 1)
    if count > 65536 or _integer(tree.get("num_cat"), "num_cat") != 0:
        raise ValueError("categorical or oversized trees are not supported")
    if _number(tree.get("shrinkage"), "shrinkage") <= 0:
        raise ValueError("invalid shrinkage metadata")
    columns = {name: [] for name in ("feature", "threshold", "left", "right", "missing", "default_left", "leaf")}
    leaf_ids, split_ids, seen_objects = set(), set(), set()

    def visit(node, depth=0):
        if depth > 256 or len(seen_objects) >= 2 * count:
            raise ValueError("invalid tree depth or node count")
        if not isinstance(node, Mapping) or id(node) in seen_objects:
            raise ValueError("invalid, shared, or cyclic tree node")
        seen_objects.add(id(node))
        position = len(columns["feature"])
        for key, default in (("feature", -1), ("threshold", 0.), ("left", -1), ("right", -1),
                             ("missing", 0), ("default_left", False), ("leaf", 0.)):
            columns[key].append(default)
        if "leaf_value" in node:
            _keys(node, _LEAF_KEYS, "constant leaf")
            leaf_index = _integer(node.get("leaf_index", 0 if count == 1 else None), "leaf_index")
            if leaf_index in leaf_ids:
                raise ValueError("duplicate leaf index")
            leaf_ids.add(leaf_index)
            columns["leaf"][position] = _number(node["leaf_value"], "leaf_value")
            return position
        _keys(node, _SPLIT_KEYS, "numerical split")
        split_index = _integer(node.get("split_index"), "split_index")
        if split_index in split_ids:
            raise ValueError("duplicate split index")
        split_ids.add(split_index)
        feature = _integer(node.get("split_feature"), "split_feature")
        if feature >= feature_count or node.get("decision_type") != "<=":
            raise ValueError("only in-range numerical <= splits are supported")
        missing = node.get("missing_type")
        if missing not in {"None", "Zero", "NaN"} or type(node.get("default_left")) is not bool:
            raise ValueError("unknown missing-value decision semantics")
        columns["feature"][position] = feature
        columns["threshold"][position] = _number(node.get("threshold"), "threshold")
        columns["missing"][position] = {"None": 0, "Zero": 1, "NaN": 2}[missing]
        columns["default_left"][position] = node["default_left"]
        columns["left"][position] = visit(node.get("left_child"), depth + 1)
        columns["right"][position] = visit(node.get("right_child"), depth + 1)
        return position

    visit(tree.get("tree_structure"))
    if leaf_ids != set(range(count)) or split_ids != set(range(count - 1)):
        raise ValueError("tree leaf/split counts do not match official indices")
    compiled = {}
    for name, values in columns.items():
        dtype = bool if name == "default_left" else np.int32 if name in {"feature", "left", "right", "missing"} else np.float64
        array = np.asarray(values, dtype=dtype)
        array.setflags(write=False)
        compiled[name] = array
    return _Tree(**compiled)


class NumericTreeRegressor:
    """An immutable compiled subset of one LightGBM regression model."""

    def __init__(self, names, trees):
        self.feature_names = tuple(names)
        self._trees = tuple(trees)

    @classmethod
    def from_dump(cls, dump: Mapping, *, expected_feature_names: Sequence[str]):
        _keys(dump, _ROOT_KEYS, "model")
        if dump.get("name") != "tree" or dump.get("version") != "v4":
            raise ValueError("only official LightGBM v4 tree dumps are supported")
        if dump.get("objective") != "regression":
            raise ValueError("only the identity-link regression objective is supported")
        if (_integer(dump.get("num_class"), "num_class", 1) != 1
                or _integer(dump.get("num_tree_per_iteration"), "num_tree_per_iteration", 1) != 1
                or dump.get("average_output") is not False):
            raise ValueError("only additive, single-output regression models are supported")
        names = _names(dump.get("feature_names"))
        if names != _names(expected_feature_names):
            raise ValueError("model feature names/order do not match the expected schema")
        if _integer(dump.get("max_feature_idx"), "max_feature_idx") != len(names) - 1:
            raise ValueError("model feature count is inconsistent")
        if dump.get("pandas_categorical") not in (None, []):
            raise ValueError("categorical feature metadata is unsupported")
        constraints = dump.get("monotone_constraints", [])
        if not isinstance(constraints, list) or any(type(value) is not int or value != 0 for value in constraints):
            raise ValueError("nonempty monotonic constraints are unsupported")
        if constraints and len(constraints) != len(names):
            raise ValueError("invalid monotonic constraint dimensions")
        infos = dump.get("feature_infos", {})
        if not isinstance(infos, Mapping) or set(infos) - set(names):
            raise ValueError("invalid feature metadata")
        for info in infos.values():
            _keys(info, {"min_value", "max_value", "values"}, "feature metadata")
            if info.get("values") != []:
                raise ValueError("categorical feature values are unsupported")
        trees = dump.get("tree_info")
        if not isinstance(trees, list) or not 1 <= len(trees) <= 20000:
            raise ValueError("invalid number of trees")
        return cls(names, [_compile_tree(tree, index, len(names)) for index, tree in enumerate(trees)])

    @classmethod
    def from_json(cls, path, *, expected_feature_names: Sequence[str]):
        """Load a bounded official dump JSON; no pickle or executable payload."""
        path = Path(path)
        if path.stat().st_size > MAX_MODEL_BYTES:
            raise ValueError("model JSON exceeds the supported size")

        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON model key")
                result[key] = value
            return result

        def invalid_constant(value):
            raise ValueError(f"nonfinite JSON model constant: {value}")

        try:
            dump = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs,
                              parse_constant=invalid_constant)
        except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
            raise ValueError("invalid model JSON") from exc
        return cls.from_dump(dump, expected_feature_names=expected_feature_names)

    @property
    def tree_count(self):
        return len(self._trees)

    def predict(self, values, *, feature_names: Sequence[str]) -> np.ndarray:
        """Return one float64 raw score per row; no processors or calibration.

        All exported trees are used, in order. Leaf values already include
        shrinkage and any training initial score; multiplying by shrinkage or
        adding another intercept would double-apply the exported model state.
        NaN and infinities preserve official numerical prediction semantics.
        """
        if _names(feature_names) != self.feature_names:
            raise ValueError("prediction feature names/order do not match the model")
        matrix = np.asarray(values)
        if matrix.dtype.kind not in "fiu" or matrix.ndim != 2 or matrix.shape[1] != len(self.feature_names):
            raise ValueError("prediction input must be a numerical matrix with the expected columns")
        matrix = matrix.astype(np.float64, copy=False)
        scores = np.zeros(len(matrix), dtype=np.float64)
        for tree in self._trees:
            active = np.arange(len(matrix))
            nodes = np.zeros(len(matrix), dtype=np.int32)
            while len(active):
                positions = nodes[active]
                features = tree.feature[positions]
                leaf_mask = features < 0
                scores[active[leaf_mask]] += tree.leaf[positions[leaf_mask]]
                active = active[~leaf_mask]
                if not len(active):
                    break
                positions = positions[~leaf_mask]
                features = features[~leaf_mask]
                observed = matrix[active, features]
                missing_type = tree.missing[positions]
                # NumericalDecision: NaN becomes zero unless missing_type=NaN.
                observed = np.where(np.isnan(observed) & (missing_type != 2), 0., observed)
                missing = (((missing_type == 1) & (np.abs(observed) <= ZERO_THRESHOLD))
                           | ((missing_type == 2) & np.isnan(observed)))
                left = np.where(missing, tree.default_left[positions], observed <= tree.threshold[positions])
                nodes[active] = np.where(left, tree.left[positions], tree.right[positions])
        return scores
