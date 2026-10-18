"""Data mining nodes based on scikit-learn.

Learners fit a scikit-learn pipeline (one-hot encoding of nominal columns,
optional scaling) and output a MODEL port; predictors apply it. Missing
values are handled in polars before the pipeline so learners and
predictors treat them identically.
"""
from __future__ import annotations

import numpy as np
import polars as pl

from verknode.node import views
from verknode.node import (
    F,
    InvalidSettingsError,
    MODEL,
    ModelPortObject,
    NodeModel,
    Port,
    TABLE,
    require_column,
)
from verknode.node import dtype_kind, is_numeric_kind, register
from .manipulation import selected_columns

NUMERIC_KINDS = ["integer", "long", "double"]
NOMINAL_KINDS = ["string", "boolean"]
CLASS_KINDS = ["string", "boolean", "integer", "long"]
MISSING_FAIL, MISSING_SKIP, MISSING_IMPUTE = "Fail", "Skip rows", "Impute (mean / most frequent)"
MISSING_OPTIONS = [MISSING_SKIP, MISSING_IMPUTE, MISSING_FAIL]
ALL_COLUMNS = {"include": [], "exclude": [], "enforce": "exclusion"}
CLASSIFICATION, REGRESSION = "Classification", "Regression"


# ----------------------------------------------------------------------
# shared model machinery
# ----------------------------------------------------------------------
class FittedModel:
    """A fitted scikit-learn pipeline plus the polars preprocessing applied before it."""

    def __init__(self, pipeline, numeric: list, nominal: list, impute: dict | None):
        self.pipeline = pipeline
        self.numeric = numeric
        self.nominal = nominal
        self.impute = impute

    def __repr__(self):
        return repr(self.estimator)

    @property
    def estimator(self):
        return self.pipeline.steps[-1][1]

    def matrix(self, df: pl.DataFrame):
        """(pandas feature frame of the complete rows, numpy mask of those rows)."""
        cols = df.select([pl.col(c).cast(pl.Float64) for c in self.numeric]
                         + [pl.col(c).cast(pl.String) for c in self.nominal])
        if self.impute:
            cols = cols.with_columns(pl.col(c).fill_null(v) for c, v in self.impute.items() if v is not None)
        valid = cols.select(pl.all_horizontal(pl.all().is_not_null()).alias("v"))["v"]
        import pandas  # noqa: F401 - (to_pandas needs it: said here, so the node's requirements show it)

        return cols.filter(valid).to_pandas(), valid.to_numpy()

    def feature_names(self) -> list:
        names = [str(n) for n in self.pipeline.steps[0][1].get_feature_names_out()]
        names = [n.split("__", 1)[1] if "__" in n else n for n in names]
        for _, step in self.pipeline.steps[1:-1]:
            if hasattr(step, "get_feature_names_out"):
                names = [str(n) for n in step.get_feature_names_out(names)]
        return names

    def transformed(self, X):
        out = X
        for _, step in self.pipeline.steps[:-1]:
            out = step.transform(out)
        return out


def feature_columns(sel, schema, exclude=(), numeric_only=False) -> list:
    allowed = NUMERIC_KINDS if numeric_only else NUMERIC_KINDS + NOMINAL_KINDS
    chosen = [c for c in selected_columns(sel, schema) if c not in exclude]
    bad = [c for c in chosen if dtype_kind(schema[c]) not in allowed]
    if bad and (sel or {}).get("enforce") == "inclusion":
        kind = "numeric" if numeric_only else "numeric, String or Boolean"
        raise InvalidSettingsError(f"Learning columns must be {kind}: {', '.join(bad)}")
    feats = [c for c in chosen if c not in bad]
    if not feats:
        raise InvalidSettingsError("No learning columns selected")
    return feats


def fit_model(df: pl.DataFrame, target, feats, mode, estimator, scale=False, drop_first=False, steps=()):
    """Fit estimator on df; returns (FittedModel, X, y, skipped_rows)."""
    from sklearn.compose import ColumnTransformer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    numeric = [c for c in feats if is_numeric_kind(dtype_kind(df.schema[c]))]
    nominal = [c for c in feats if c not in numeric]
    data = df.filter(pl.col(target).is_not_null()) if target else df
    skipped = df.height - data.height
    if mode == MISSING_FAIL and data.select(feats).null_count().sum_horizontal().item() > 0:
        raise ValueError("Missing values in learning columns (choose 'Skip rows' or 'Impute')")
    impute = None
    if mode == MISSING_IMPUTE:
        impute = {c: data[c].cast(pl.Float64).mean() for c in numeric}
        for c in nominal:
            m = data[c].cast(pl.String).drop_nulls().mode().sort()
            impute[c] = m[0] if m.len() else None
    fm = FittedModel(None, numeric, nominal, impute)
    X, valid = fm.matrix(data)
    skipped += int((~valid).sum())
    if X.shape[0] == 0:
        raise ValueError("No rows to learn from (all rows have missing values?)")
    y = data[target].filter(pl.Series(valid)).to_numpy() if target else None
    transformers = []
    if numeric:
        transformers.append(("num", StandardScaler() if scale else "passthrough", numeric))
    if nominal:
        transformers.append(("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False,
                                                  drop="first" if drop_first else None), nominal))
    pipeline = Pipeline([("prep", ColumnTransformer(transformers)), *steps, ("model", estimator)])
    pipeline.fit(X, y)
    fm.pipeline = pipeline
    return fm, X, y, skipped


def check_model(model, accepts):
    if not isinstance(model, ModelPortObject):
        raise InvalidSettingsError("No model at the model input port")
    if accepts and model.model_type not in accepts:
        raise InvalidSettingsError(
            f'Model of type "{model.model_type}" is not supported (expected {" / ".join(accepts)})')


def check_features(model, df):
    """df: DataFrame or schema."""
    names = df.columns if isinstance(df, pl.DataFrame) else list(df)
    missing = [c for c in model.features if c not in names]
    if missing:
        raise InvalidSettingsError(f"Learning columns missing in input table: {', '.join(missing)}")


def scatter(values, valid, n, dtype):
    """Values computed for the valid rows -> full column with missing values elsewhere."""
    out = [None] * n
    for i, v in zip(np.flatnonzero(valid), values):
        out[i] = v.item() if hasattr(v, "item") else v
    return pl.Series(out, dtype=dtype)


def add_columns(df: pl.DataFrame, cols: list) -> pl.DataFrame:
    for c in cols:
        if c.name in df.columns:
            raise InvalidSettingsError(f'Column "{c.name}" already exists')
    return df.with_columns(cols)


def predict_columns(model: ModelPortObject, df: pl.DataFrame, s: dict, name: str) -> list:
    """Prediction (+ confidence / class probability) columns for df."""
    classification = model.info.get("Task") == CLASSIFICATION
    dtype = model.target_dtype if classification else pl.Float64
    fm = model.estimator
    n = df.height
    cols = []
    if fm is None:  # configure dry run: typed missing values
        cols.append(pl.Series(name, [None] * n, dtype=dtype))
        classes = model.classes or []
        proba = None
    else:
        X, valid = fm.matrix(df)
        pred = fm.pipeline.predict(X) if X.shape[0] else []
        cols.append(scatter(pred, valid, n, dtype).alias(name))
        classes = list(fm.pipeline.classes_) if classification else []
        proba = fm.pipeline.predict_proba(X) if classification and X.shape[0] and (
            s.get("append_probabilities") or s.get("append_confidence")) else None
    if classification and s.get("append_confidence"):
        conf = proba.max(axis=1) if proba is not None else []
        cols.append(scatter(conf, valid, n, pl.Float64).alias("Prediction confidence") if fm is not None
                    else pl.Series("Prediction confidence", [None] * n, dtype=pl.Float64))
    if classification and s.get("append_probabilities"):
        suffix = s.get("prob_suffix") or ""
        for j, cls in enumerate(classes):
            col = f"P ({model.target}={_label(cls)}){suffix}"
            if proba is not None:
                cols.append(scatter(proba[:, j], valid, n, pl.Float64).alias(col))
            else:
                cols.append(pl.Series(col, [None] * n, dtype=pl.Float64))
    return cols


def _label(v):
    return v.item() if hasattr(v, "item") else v


def _simple_params(est) -> dict:
    return {k: v for k, v in est.get_params().items() if isinstance(v, (int, float, str, bool, tuple)) or v is None}


def importance_chart(names, values, title="Feature importance") -> str:
    order = np.argsort(values)[::-1][:20][::-1]
    fig, ax = views.figure(7, max(2.5, 0.3 * len(order) + 1))
    ax.barh([names[i] for i in order], [values[i] for i in order], color=views.PALETTE[0])
    ax.set_title(title)
    return views.chart(views.svg(fig))


# ----------------------------------------------------------------------
# learner / predictor base classes
# ----------------------------------------------------------------------
def learner_fields(task, numeric_only=False):
    kinds = CLASS_KINDS if task == CLASSIFICATION else NUMERIC_KINDS
    if task == "auto":
        kinds = CLASS_KINDS + ["double"]
    feat_help = "Numeric columns" if numeric_only else "Numeric, String and Boolean columns (nominal columns are one-hot encoded)"
    return [
        F("target", "column", "Target column", None,
          "Nominal class column" if task == CLASSIFICATION else "Numeric column to predict"
          if task == REGRESSION else "Nominal column (classification) or Double column (regression)", kinds=kinds),
        F("features", "columns", "Learning columns", dict(ALL_COLUMNS), f"{feat_help}; the target is excluded."),
        F("missing", "enum", "Missing values", MISSING_SKIP,
          "Skip rows: rows with missing values are not learned / get missing predictions. "
          "Impute: missing values are replaced by the training mean (numbers) or most frequent value.",
          options=MISSING_OPTIONS),
    ]


def predictor_fields(classification=True):
    fields = [
        F("change_prediction", "bool", "Change prediction column name", False),
        F("prediction_column", "string", "Prediction column name", "Prediction",
          show_if={"change_prediction": True}),
    ]
    if classification:
        fields += [
            F("append_confidence", "bool", "Append overall prediction confidence", False),
            F("append_probabilities", "bool", "Append individual class probabilities", False),
            F("prob_suffix", "string", "Suffix for probability columns", "", show_if={"append_probabilities": True}),
        ]
    return fields


class Learner(NodeModel):
    abstract = True  # base class, not a node
    node_type = "Learner"
    has_view = True
    task = CLASSIFICATION  # CLASSIFICATION | REGRESSION | "auto" (by target type)
    model_type = ""
    numeric_only = False
    scale = False
    drop_first = False
    in_ports = [Port(TABLE, "Training data")]
    out_ports = [Port(MODEL, "Model", "Trained model for the predictor")]

    def make_estimator(self, s, task):
        raise NotImplementedError

    def steps(self, s):
        return ()

    def extra_specs(self, feats_schema, target, s, task) -> list:
        return []

    def extra_outputs(self, fm, X, y, s, task, model) -> list:
        return []

    def view_parts(self, fm, X, y, s, task) -> list:
        return []

    def _task(self, schema, target):
        if self.task != "auto":
            return self.task
        return CLASSIFICATION if dtype_kind(schema[target]) in ("string", "boolean") else REGRESSION

    def _columns(self, schema, s):
        target = require_column(s, "target", schema, "target column")
        kind = dtype_kind(schema[target])
        task = self._task(schema, target)
        allowed = CLASS_KINDS if task == CLASSIFICATION else NUMERIC_KINDS
        if kind not in allowed:
            raise InvalidSettingsError(f'Target column "{target}" must be '
                                       f'{"nominal" if task == CLASSIFICATION else "numeric"}')
        feats = feature_columns(s["features"], schema, {target}, self.numeric_only)
        return target, feats, task

    def configure(self, in_specs, s, ctx):
        schema = in_specs[0]
        target, feats, task = self._columns(schema, s)
        self.make_estimator(s, task)  # validates parameters
        fschema = pl.Schema({c: schema[c] for c in feats})
        spec = ModelPortObject(self.model_type, None, fschema, target, schema[target], None, {"Task": task})
        return [spec, *self.extra_specs(fschema, target, s, task)]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        target, feats, task = self._columns(df.schema, s)
        fm, X, y, skipped = fit_model(df, target, feats, s["missing"], self.make_estimator(s, task),
                                      self.scale, self.drop_first, self.steps(s))
        if skipped:
            ctx.warn(f"{skipped} row(s) with missing values were not used for learning")
        info = {"Task": task, "Training rows": int(X.shape[0])}
        pred = fm.pipeline.predict(X)
        if task == CLASSIFICATION:
            info["Training accuracy"] = round(float((pred == y).mean()), 6)
            classes = [_label(c) for c in fm.pipeline.classes_]
        else:
            from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

            info["Training R²"] = round(float(r2_score(y, pred)), 6)
            info["Training RMSE"] = round(float(np.sqrt(mean_squared_error(y, pred))), 6)
            info["Training MAE"] = round(float(mean_absolute_error(y, pred)), 6)
            classes = None
        fschema = pl.Schema({c: df.schema[c] for c in feats})
        model = ModelPortObject(self.model_type, fm, fschema, target, df.schema[target], classes, info)
        extra = self.extra_outputs(fm, X, y, s, task, model)
        parts = [views.key_values({**info, **_simple_params(fm.estimator)})]
        est = fm.estimator
        if hasattr(est, "feature_importances_"):
            parts.append(importance_chart(fm.feature_names(), list(est.feature_importances_)))
        parts.extend(self.view_parts(fm, X, y, s, task))
        ctx.set_view(views.page(f"{self.name} - {self.model_type}", *parts))
        ctx.log(f"Learned {self.model_type} model from {X.shape[0]} rows")
        return [model, *extra]


class Predictor(NodeModel):
    abstract = True  # base class, not a node
    node_type = "Predictor"
    accepts: tuple = ()
    classification = True
    in_ports = [Port(MODEL, "Model", "Model from the learner"), Port(TABLE, "Data to predict")]
    out_ports = [Port(TABLE, "Predicted data", "Input table with prediction columns")]

    def execute(self, inputs, s, ctx):
        model, df = inputs
        check_model(model, self.accepts)
        check_features(model, df)
        name = s["prediction_column"] if s.get("change_prediction") and s.get("prediction_column") \
            else f"Prediction ({model.target})"
        return [add_columns(df, predict_columns(model, df, s, name))]


def pair(prefix, model_type, task, category, icon, learner_doc, estimator_fn, est_fields, learner_name,
         predictor_name, accepts=None, bases=(), learner_factory=None, predictor_factory=None, **learner_attrs):
    """Create and register a learner / predictor class pair."""
    lcls = type(prefix + "Learner", (*bases, Learner), {
        "factory": learner_factory or f"verknode.mining.{prefix}Learner",
        "name": learner_name,
        "category": category,
        "icon": icon,
        "model_type": model_type,
        "task": task,
        "short_description": learner_doc[0],
        "description": learner_doc[1],
        "fields": learner_fields(task, learner_attrs.get("numeric_only", False)) + est_fields,
        "make_estimator": lambda self, s, t: estimator_fn(s, t),
        **learner_attrs,
    })
    pcls = type(prefix + "Predictor", (Predictor,), {
        "factory": predictor_factory or f"verknode.mining.{prefix}Predictor",
        "name": predictor_name,
        "category": category,
        "icon": (icon + "▸")[:4],
        "accepts": accepts or (model_type,),
        "classification": task != REGRESSION,
        "short_description": f"Applies a {model_type} model.",
        "description": f"Predicts the target of the input rows using a model from the <i>{learner_name}</i>.",
        "fields": predictor_fields(task != REGRESSION),
    })
    register(lcls)
    register(pcls)
    return lcls, pcls


def _depth(v):
    return None if not v or v <= 0 else int(v)


def _seed(s):
    return None if s.get("seed", -1) == -1 else s["seed"]


SEED = F("seed", "int", "Random seed (-1: none)", 42)


# ----------------------------------------------------------------------
# Decision trees
# ----------------------------------------------------------------------
QUALITY = {"Gini index": "gini", "Information gain (entropy)": "entropy"}


def _dt(s, task):
    from sklearn.tree import DecisionTreeClassifier

    if s["min_records"] < 2:
        raise InvalidSettingsError("Min number records per node must be at least 2")
    return DecisionTreeClassifier(criterion=QUALITY[s["quality"]], max_depth=_depth(s["max_depth"]),
                                  min_samples_split=s["min_records"], ccp_alpha=max(0.0, s["ccp_alpha"]),
                                  random_state=_seed(s))


class _TreeView:
    def view_parts(self, fm, X, y, s, task):
        from sklearn.tree import export_text

        text = export_text(fm.estimator, feature_names=fm.feature_names(), max_depth=8, decimals=3)
        return [f"<h2>Tree</h2><pre>{views.escape(text)}</pre>"]


DecisionTreeLearner, DecisionTreePredictor = pair(
    "DecisionTree", "Decision Tree", CLASSIFICATION, "Analytics/Mining/Decision Tree", "DT",
    ("Induces a classification decision tree.",
     "Learns a decision tree (CART) predicting the nominal target column. Nominal learning columns are "
     "one-hot encoded. The view shows the tree and the feature importances."),
    _dt,
    [F("quality", "enum", "Quality measure", "Gini index", options=list(QUALITY)),
     F("max_depth", "int", "Maximum tree depth (0 = unlimited)", 0),
     F("min_records", "int", "Min number records per node", 2, "Minimum rows required to split a node."),
     F("ccp_alpha", "float", "Pruning (cost-complexity alpha)", 0.0, "0 = no pruning; larger values prune more."),
     SEED],
    "Decision Tree Learner", "Decision Tree Predictor", bases=(_TreeView,))


def _rt(s, task):
    from sklearn.tree import DecisionTreeRegressor

    return DecisionTreeRegressor(max_depth=_depth(s["max_depth"]), min_samples_split=max(2, s["min_split"]),
                                 min_samples_leaf=max(1, s["min_leaf"]), random_state=_seed(s))


SimpleRegressionTreeLearner, SimpleRegressionTreePredictor = pair(
    "SimpleRegressionTree", "Simple Regression Tree", REGRESSION, "Analytics/Mining/Decision Tree", "RT",
    ("Learns a single regression tree.", "Learns a regression tree (CART) predicting a numeric target column."),
    _rt,
    [F("max_depth", "int", "Limit number of levels (tree depth, 0 = unlimited)", 10),
     F("min_split", "int", "Minimum split node size", 2),
     F("min_leaf", "int", "Minimum child node size", 1),
     SEED],
    "Simple Regression Tree Learner", "Simple Regression Tree Predictor", bases=(_TreeView,))


# ----------------------------------------------------------------------
# Tree ensembles
# ----------------------------------------------------------------------
def _rf(s, task):
    from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor

    if s["n_models"] < 1:
        raise InvalidSettingsError("Number of models must be at least 1")
    kw = dict(n_estimators=s["n_models"], max_depth=_depth(s["max_depth"]), random_state=_seed(s),
              max_features={"Square root": "sqrt", "All columns": None, "Log2": "log2"}[s["attributes"]], n_jobs=-1)
    if task == CLASSIFICATION:
        return RandomForestClassifier(criterion=QUALITY[s["quality"]], **kw)
    return RandomForestRegressor(**kw)


RF_FIELDS = [
    F("n_models", "int", "Number of models", 100),
    F("max_depth", "int", "Limit number of levels (tree depth, 0 = unlimited)", 0),
    F("attributes", "enum", "Attribute sampling (columns)", "Square root", options=["Square root", "Log2", "All columns"]),
    SEED,
]
pair("RandomForest", "Random Forest", CLASSIFICATION, "Analytics/Mining/Decision Tree Ensemble/Random Forest", "RF",
     ("Learns a random forest for classification.",
      "Learns an ensemble of decision trees on bootstrap samples with random attribute selection."),
     _rf, [F("quality", "enum", "Split criterion", "Gini index", options=list(QUALITY))] + RF_FIELDS,
     "Random Forest Learner", "Random Forest Predictor")
pair("RandomForestRegression", "Random Forest (Regression)", REGRESSION,
     "Analytics/Mining/Decision Tree Ensemble/Random Forest", "RFR",
     ("Learns a random forest for regression.", "Learns an ensemble of regression trees (random forest)."),
     _rf, RF_FIELDS, "Random Forest Learner (Regression)", "Random Forest Predictor (Regression)")


def _gbt(s, task):
    from sklearn.ensemble import GradientBoostingClassifier, GradientBoostingRegressor

    if not 0 < s["learning_rate"] <= 1:
        raise InvalidSettingsError("Learning rate must be in (0, 1]")
    cls = GradientBoostingClassifier if task == CLASSIFICATION else GradientBoostingRegressor
    return cls(n_estimators=max(1, s["n_models"]), learning_rate=s["learning_rate"],
               max_depth=_depth(s["max_depth"]) or 4, subsample=min(1.0, max(0.01, s["subsample"])),
               random_state=_seed(s))


GBT_FIELDS = [
    F("n_models", "int", "Number of models", 100),
    F("learning_rate", "float", "Learning rate", 0.1),
    F("max_depth", "int", "Limit number of levels (tree depth)", 4),
    F("subsample", "float", "Data sampling (rows fraction)", 1.0),
    SEED,
]
pair("GradientBoostedTrees", "Gradient Boosted Trees", CLASSIFICATION,
     "Analytics/Mining/Decision Tree Ensemble/Gradient Boosting", "GBT",
     ("Learns gradient boosted trees for classification.",
      "Learns an ensemble of shallow regression trees with gradient boosting (classification)."),
     _gbt, GBT_FIELDS, "Gradient Boosted Trees Learner", "Gradient Boosted Trees Predictor")
pair("GradientBoostedTreesRegression", "Gradient Boosted Trees (Regression)", REGRESSION,
     "Analytics/Mining/Decision Tree Ensemble/Gradient Boosting", "GBR",
     ("Learns gradient boosted trees for regression.", "Learns gradient boosted regression trees."),
     _gbt, GBT_FIELDS, "Gradient Boosted Trees Learner (Regression)", "Gradient Boosted Trees Predictor (Regression)")


# ----------------------------------------------------------------------
# Naive Bayes
# ----------------------------------------------------------------------
def _nb(s, task):
    from sklearn.naive_bayes import GaussianNB

    if s["var_smoothing"] <= 0:
        raise InvalidSettingsError("Variance smoothing must be positive")
    return GaussianNB(var_smoothing=s["var_smoothing"])


pair("NaiveBayes", "Naive Bayes", CLASSIFICATION, "Analytics/Mining/Bayes", "NB",
     ("Learns a Naive Bayes model.",
      "Learns a (Gaussian) Naive Bayes classifier; nominal columns are one-hot encoded."),
     _nb, [F("var_smoothing", "float", "Variance smoothing", 1e-9,
             "Portion of the largest variance added to all variances (avoids zero standard deviation).")],
     "Naive Bayes Learner", "Naive Bayes Predictor")


# ----------------------------------------------------------------------
# Linear / polynomial / logistic regression
# ----------------------------------------------------------------------
COEFF_SCHEMA = pl.Schema({"Variable": pl.String, "Coeff.": pl.Float64, "Std. Err.": pl.Float64,
                          "t-value": pl.Float64, "P>|t|": pl.Float64})
REG_CATEGORY = "Analytics/Mining/Linear & Polynomial Regression"


class _OLSCoefficients:
    out_ports = [Port(MODEL, "Model", "Regression model"),
                 Port(TABLE, "Coefficients and Statistics", "Coefficients with standard error, t-value and p-value")]

    def extra_specs(self, feats_schema, target, s, task):
        return [COEFF_SCHEMA]

    def extra_outputs(self, fm, X, y, s, task, model):
        import statsmodels.api as sm

        design = sm.add_constant(np.asarray(fm.transformed(X), dtype=float), has_constant="add")
        res = sm.OLS(np.asarray(y, dtype=float), design).fit()
        names = ["Intercept"] + fm.feature_names()
        table = pl.DataFrame({
            "Variable": pl.Series(names, dtype=pl.String),
            "Coeff.": pl.Series(np.asarray(res.params, dtype=float), dtype=pl.Float64),
            "Std. Err.": pl.Series(np.asarray(res.bse, dtype=float), dtype=pl.Float64),
            "t-value": pl.Series(np.asarray(res.tvalues, dtype=float), dtype=pl.Float64),
            "P>|t|": pl.Series(np.asarray(res.pvalues, dtype=float), dtype=pl.Float64),
        }).with_columns(pl.selectors.float().fill_nan(None))
        model.info["R²"] = round(float(res.rsquared), 6)
        model.info["Adjusted R²"] = round(float(res.rsquared_adj), 6)
        self._table = table
        return [table]

    def view_parts(self, fm, X, y, s, task):
        return ["<h2>Coefficients and Statistics</h2>", views.table(self._table)]


def _linreg(s, task):
    from sklearn.linear_model import LinearRegression

    return LinearRegression()


LinearRegressionLearner, RegressionPredictor = pair(
    "LinearRegression", "Linear Regression", REGRESSION, REG_CATEGORY, "LR",
    ("Performs a multivariate linear regression.",
     "Fits a linear regression (ordinary least squares). Nominal columns are dummy-coded with the first "
     "category as reference. The second output holds coefficients with standard errors and p-values."),
    _linreg, [], "Linear Regression Learner", "Regression Predictor",
    accepts=("Linear Regression", "Polynomial Regression"), bases=(_OLSCoefficients,), drop_first=True,
    predictor_factory="verknode.mining.RegressionPredictor")
RegressionPredictor.description = "Predicts numeric values using a linear or polynomial regression model."
RegressionPredictor.short_description = "Applies a linear or polynomial regression model."


def _polyreg(s, task):
    from sklearn.linear_model import LinearRegression

    if not 1 <= s["degree"] <= 10:
        raise InvalidSettingsError("Maximum polynomial degree must be between 1 and 10")
    return LinearRegression()


class PolynomialRegressionLearner(_OLSCoefficients, Learner):
    factory = "verknode.mining.PolynomialRegressionLearner"
    name = "Polynomial Regression Learner"
    category = REG_CATEGORY
    icon = "x²"
    model_type = "Polynomial Regression"
    task = REGRESSION
    numeric_only = True
    short_description = "Learns a polynomial regression model."
    description = ("Fits a polynomial of the numeric learning columns (including interaction terms) up to "
                   "the maximum degree with least squares. Apply it with the <i>Regression Predictor</i>.")
    fields = learner_fields(REGRESSION, numeric_only=True) + [
        F("degree", "int", "Maximum polynomial degree", 2)]

    def make_estimator(self, s, task):
        return _polyreg(s, task)

    def steps(self, s):
        from sklearn.preprocessing import PolynomialFeatures

        return (("poly", PolynomialFeatures(degree=s["degree"], include_bias=False)),)


LOGIT_SCHEMA = pl.Schema({"Logit": pl.String, "Variable": pl.String, "Coeff.": pl.Float64, "Odds ratio": pl.Float64})
PENALTY = {"None": None, "L2 (Ridge)": "l2", "L1 (Lasso)": "l1"}


class LogisticRegressionLearner(Learner):
    factory = "verknode.mining.LogisticRegressionLearner"
    name = "Logistic Regression Learner"
    category = "Analytics/Mining/Logistic Regression"
    icon = "LoR"
    model_type = "Logistic Regression"
    drop_first = True
    short_description = "Learns a (multinomial) logistic regression model."
    description = ("Fits a multinomial logistic regression for a nominal target. Coefficients are reported "
                   "relative to the reference category (default: the last category).")
    out_ports = [Port(MODEL, "Model", "Logistic regression model"),
                 Port(TABLE, "Coefficients and Statistics", "Coefficients per logit (target category vs. reference)")]
    fields = learner_fields(CLASSIFICATION) + [
        F("reference", "string", "Reference category", "", "Empty: last category (sorted)."),
        F("penalty", "enum", "Regularization", "None", options=list(PENALTY)),
        F("C", "float", "Inverse regularization strength (C)", 1.0, show_if={"penalty": ["L2 (Ridge)", "L1 (Lasso)"]}),
        F("max_iter", "int", "Maximum number of epochs", 1000),
    ]

    def make_estimator(self, s, task):
        from sklearn.linear_model import LogisticRegression

        if s["C"] <= 0:
            raise InvalidSettingsError("C must be positive")
        penalty = PENALTY[s["penalty"]]
        return LogisticRegression(penalty=penalty, C=s["C"] if penalty else 1.0, max_iter=max(10, s["max_iter"]),
                                  solver="saga" if penalty == "l1" else "lbfgs")

    def extra_specs(self, feats_schema, target, s, task):
        return [LOGIT_SCHEMA]

    def extra_outputs(self, fm, X, y, s, task, model):
        est = fm.estimator
        classes = [_label(c) for c in est.classes_]
        ref = s["reference"] or str(classes[-1])
        matches = [i for i, c in enumerate(classes) if str(c) == ref]
        if not matches:
            raise InvalidSettingsError(f"Reference category {ref!r} not found in target column")
        r = matches[0]
        names = ["Intercept"] + fm.feature_names()
        if len(classes) == 2:  # sklearn: one logit for classes[1] vs classes[0]
            sign, other = (1, 1) if r == 0 else (-1, 0)
            coef = np.zeros((2, est.coef_.shape[1]))
            coef[other] = sign * est.coef_[0]
            icpt = np.zeros(2)
            icpt[other] = sign * est.intercept_[0]
        else:
            coef = est.coef_ - est.coef_[r]
            icpt = est.intercept_ - est.intercept_[r]
        rows = {"Logit": [], "Variable": [], "Coeff.": []}
        for i, c in enumerate(classes):
            if i == r:
                continue
            values = [icpt[i]] + list(coef[i])
            rows["Logit"] += [f"{c} vs. {classes[r]}"] * len(values)
            rows["Variable"] += names
            rows["Coeff."] += [float(v) for v in values]
        table = pl.DataFrame({
            "Logit": pl.Series(rows["Logit"], dtype=pl.String),
            "Variable": pl.Series(rows["Variable"], dtype=pl.String),
            "Coeff.": pl.Series(rows["Coeff."], dtype=pl.Float64),
        }).with_columns(pl.col("Coeff.").exp().alias("Odds ratio"))
        model.info["Reference category"] = str(classes[r])
        self._table = table
        return [table]

    def view_parts(self, fm, X, y, s, task):
        return ["<h2>Coefficients</h2>", views.table(self._table)]


class LogisticRegressionPredictor(Predictor):
    factory = "verknode.mining.LogisticRegressionPredictor"
    name = "Logistic Regression Predictor"
    category = "Analytics/Mining/Logistic Regression"
    icon = "LoR▸"
    accepts = ("Logistic Regression",)
    short_description = "Applies a logistic regression model."
    description = "Predicts the target category using a model from the <i>Logistic Regression Learner</i>."
    fields = predictor_fields(True)


# ----------------------------------------------------------------------
# SVM / MLP
# ----------------------------------------------------------------------
KERNELS = {"RBF": "rbf", "Polynomial": "poly", "Linear": "linear", "Sigmoid": "sigmoid"}


def _svm(s, task):
    from sklearn.svm import SVC

    if s["C"] <= 0:
        raise InvalidSettingsError("Overlapping penalty (C) must be positive")
    gamma = "scale" if s["gamma"] <= 0 else s["gamma"]
    return SVC(kernel=KERNELS[s["kernel"]], C=s["C"], gamma=gamma, degree=max(1, s["degree"]),
               probability=True, random_state=0)


pair("SVM", "SVM", CLASSIFICATION, "Analytics/Mining/SVM", "SVM",
     ("Trains a support vector machine.",
      "Trains a support vector machine classifier. Numeric columns are standardized; nominal columns one-hot encoded."),
     _svm,
     [F("kernel", "enum", "Kernel", "RBF", options=list(KERNELS)),
      F("C", "float", "Overlapping penalty (C)", 1.0),
      F("gamma", "float", "Gamma (0 = automatic)", 0.0, show_if={"kernel": ["RBF", "Polynomial", "Sigmoid"]}),
      F("degree", "int", "Power (polynomial degree)", 3, show_if={"kernel": "Polynomial"})],
     "SVM Learner", "SVM Predictor", scale=True)


def _mlp(s, task):
    from sklearn.neural_network import MLPClassifier, MLPRegressor

    if s["layers"] < 1 or s["neurons"] < 1:
        raise InvalidSettingsError("Number of hidden layers and neurons must be at least 1")
    cls = MLPClassifier if task == CLASSIFICATION else MLPRegressor
    return cls(hidden_layer_sizes=(s["neurons"],) * s["layers"], max_iter=max(1, s["max_iter"]),
               random_state=_seed(s))


_mlp_l, _mlp_p = pair(
    "MLP", "MLP", "auto", "Analytics/Mining/Neural Network/MLP", "MLP",
    ("Trains a multilayer perceptron.",
     "Trains a feed-forward neural network (multilayer perceptron). A nominal target gives a classifier, a "
     "Double target a regression network. Numeric columns are standardized."),
    _mlp,
    [F("max_iter", "int", "Maximum number of iterations", 200),
     F("layers", "int", "Number of hidden layers", 1),
     F("neurons", "int", "Number of hidden neurons per layer", 10),
     SEED],
    "RProp MLP Learner", "MultiLayerPerceptron Predictor", scale=True,
    learner_factory="verknode.mining.RPropMLPLearner", predictor_factory="verknode.mining.MultiLayerPerceptronPredictor")
_mlp_p.fields = predictor_fields(True)  # classification options apply when the model is a classifier


# ----------------------------------------------------------------------
# k Nearest Neighbor (single node)
# ----------------------------------------------------------------------
class KNearestNeighbor(NodeModel):
    factory = "verknode.mining.KNearestNeighbor"
    name = "K Nearest Neighbor"
    category = "Analytics/Mining/Misc Classifiers"
    node_type = "Predictor"
    icon = "kNN"
    short_description = "Classifies test rows by their k nearest training rows."
    description = ("Classifies each row of the test table (second input) by a majority vote of its k nearest "
                   "neighbors in the training table (Euclidean distance on the learning columns).")
    in_ports = [Port(TABLE, "Training data"), Port(TABLE, "Test data")]
    out_ports = [Port(TABLE, "Classified data", "Test data with predicted class")]
    fields = [
        F("target", "column", "Column with class labels", None, kinds=CLASS_KINDS),
        F("features", "columns", "Learning columns", dict(ALL_COLUMNS), "Must also exist in the test table."),
        F("k", "int", "Number of neighbours to consider (k)", 3),
        F("weighted", "bool", "Weight neighbours by distance", False),
        F("append_probabilities", "bool", "Output class probabilities", False),
        F("missing", "enum", "Missing values", MISSING_SKIP, options=MISSING_OPTIONS),
    ]

    def _cols(self, schema, s):
        target = require_column(s, "target", schema, "class column")
        return target, feature_columns(s["features"], schema, {target})

    def configure(self, in_specs, s, ctx):
        train, test = in_specs
        target, feats = self._cols(train, s)
        if s["k"] < 1:
            raise InvalidSettingsError("k must be at least 1")
        missing = [c for c in feats if c not in test]
        if missing:
            raise InvalidSettingsError(f"Learning columns missing in test table: {', '.join(missing)}")
        return [pl.Schema({**test, "Class [kNN]": train[target]})]

    def execute(self, inputs, s, ctx):
        from sklearn.neighbors import KNeighborsClassifier

        train, test = inputs
        target, feats = self._cols(train.schema, s)
        if s["k"] < 1:
            raise InvalidSettingsError("k must be at least 1")
        est = KNeighborsClassifier(n_neighbors=s["k"], weights="distance" if s["weighted"] else "uniform")
        if train.height == 0:
            fm, classes, info = None, None, {"Task": CLASSIFICATION}
        else:
            fm, X, _, skipped = fit_model(train, target, feats, s["missing"], est)
            if est.n_neighbors > X.shape[0]:
                est.n_neighbors = X.shape[0]
                ctx.warn(f"k reduced to the number of training rows ({X.shape[0]})")
            classes = [_label(c) for c in fm.pipeline.classes_]
            info = {"Task": CLASSIFICATION}
            if skipped:
                ctx.warn(f"{skipped} training row(s) with missing values were skipped")
        model = ModelPortObject("kNN", fm, pl.Schema({c: train.schema[c] for c in feats}), target,
                                train.schema[target], classes, info)
        return [add_columns(test, predict_columns(model, test, s, "Class [kNN]"))]


# ----------------------------------------------------------------------
# Clustering
# ----------------------------------------------------------------------
CLUSTER_CATEGORY = "Analytics/Mining/Clustering"


def _numeric_features(s, schema):
    return feature_columns(s["features"], schema, numeric_only=True)


def _cluster_fields():
    return [
        F("features", "columns", "Used columns", dict(ALL_COLUMNS), "Numeric columns used for clustering."),
        F("missing", "enum", "Missing values", MISSING_SKIP,
          "Skip rows: rows with missing values get a missing cluster.", options=MISSING_OPTIONS),
    ]


def _prep_only(df, feats, mode):
    """FittedModel with an identity pipeline for preprocessing / imputation only."""
    from sklearn.preprocessing import FunctionTransformer

    fm, X, _, skipped = fit_model(df, None, feats, mode, FunctionTransformer())
    return fm, X, skipped


def _cluster_names(labels):
    return [None if l is None else ("Noise" if l < 0 else f"cluster_{l}") for l in labels]


class KMeans(NodeModel):
    factory = "verknode.mining.KMeans"
    name = "k-Means"
    category = CLUSTER_CATEGORY
    node_type = "Learner"
    icon = "kM"
    has_view = True
    short_description = "Clusters rows with the k-Means algorithm."
    description = ("Assigns each row to one of k clusters (column <code>Cluster</code>, values cluster_0, ...). "
                   "Outputs the labeled data, the cluster centers and a model for the <i>Cluster Assigner</i>.")
    in_ports = [Port(TABLE, "Clustering input")]
    out_ports = [Port(TABLE, "Labeled input", "Input data with Cluster column"),
                 Port(TABLE, "Clusters", "Cluster centers"),
                 Port(MODEL, "Cluster model", "Model for the Cluster Assigner")]
    fields = [F("k", "int", "Number of clusters", 3), F("max_iter", "int", "Max. number of iterations", 99), SEED] \
        + _cluster_fields()

    def configure(self, in_specs, s, ctx):
        schema = in_specs[0]
        feats = _numeric_features(s, schema)
        if s["k"] < 1:
            raise InvalidSettingsError("Number of clusters must be at least 1")
        fschema = pl.Schema({c: schema[c] for c in feats})
        return [pl.Schema({**schema, "Cluster": pl.String}),
                pl.Schema({"Cluster": pl.String, **{c: pl.Float64 for c in feats}}),
                ModelPortObject("k-Means", None, fschema, None, None, None, {"Task": "Clustering"})]

    def execute(self, inputs, s, ctx):
        from sklearn.cluster import KMeans as SKKMeans

        df = inputs[0]
        feats = _numeric_features(s, df.schema)
        k = s["k"]
        if k < 1:
            raise InvalidSettingsError("Number of clusters must be at least 1")
        fm, X, skipped = _prep_only(df, feats, s["missing"])
        if X.shape[0] < k:
            raise ValueError(f"Fewer rows ({X.shape[0]}) than clusters ({k})")
        est = SKKMeans(n_clusters=k, max_iter=max(1, s["max_iter"]), random_state=_seed(s), n_init=10)
        fm.pipeline.steps[-1] = ("model", est)
        fm.pipeline.fit(X)
        _, valid = fm.matrix(df)
        labels = scatter(est.labels_, valid, df.height, pl.Int64).to_list()
        labeled = df.with_columns(pl.Series("Cluster", _cluster_names(labels), dtype=pl.String))
        centers = pl.DataFrame({"Cluster": pl.Series([f"cluster_{i}" for i in range(k)], dtype=pl.String),
                                **{c: pl.Series(est.cluster_centers_[:, j], dtype=pl.Float64) for j, c in enumerate(feats)}})
        info = {"Task": "Clustering", "Number of clusters": k, "Inertia": round(float(est.inertia_), 6)}
        model = ModelPortObject("k-Means", fm, pl.Schema({c: df.schema[c] for c in feats}), None, None,
                                [f"cluster_{i}" for i in range(k)], info)
        if skipped:
            ctx.warn(f"{skipped} row(s) with missing values were not clustered")
        sizes = labeled.group_by("Cluster", maintain_order=True).len().sort("Cluster")
        parts = [views.key_values(info), "<h2>Cluster centers</h2>", views.table(centers),
                 "<h2>Cluster sizes</h2>", views.table(sizes)]
        if len(feats) >= 2:
            fig, ax = views.figure(6, 4.5)
            pts = X.to_numpy()
            ax.scatter(pts[:, 0], pts[:, 1], c=[views.PALETTE[l % 10] for l in est.labels_], s=14)
            ax.scatter(est.cluster_centers_[:, 0], est.cluster_centers_[:, 1], c="black", marker="x", s=80)
            ax.set_xlabel(feats[0])
            ax.set_ylabel(feats[1])
            parts.insert(1, views.chart(views.svg(fig)))
        ctx.set_view(views.page("k-Means", *parts))
        return [labeled, centers, model]


class ClusterAssigner(NodeModel):
    factory = "verknode.mining.ClusterAssigner"
    name = "Cluster Assigner"
    category = CLUSTER_CATEGORY
    node_type = "Predictor"
    icon = "C▸"
    short_description = "Assigns rows to the nearest cluster of a cluster model."
    description = "Assigns each row to the closest cluster center of a model from the <i>k-Means</i> node."
    in_ports = [Port(MODEL, "Cluster model"), Port(TABLE, "Input data")]
    out_ports = [Port(TABLE, "Labeled data", "Input data with Cluster column")]
    fields = []

    def execute(self, inputs, s, ctx):
        model, df = inputs
        check_model(model, ("k-Means",))
        check_features(model, df)
        fm = model.estimator
        if fm is None:
            col = pl.Series("Cluster", [None] * df.height, dtype=pl.String)
        else:
            X, valid = fm.matrix(df)
            labels = scatter(fm.pipeline.predict(X) if X.shape[0] else [], valid, df.height, pl.Int64).to_list()
            col = pl.Series("Cluster", _cluster_names(labels), dtype=pl.String)
        return [add_columns(df, [col])]


DISTANCES = {"Euclidean": "euclidean", "Manhattan": "cityblock"}


class DBSCAN(NodeModel):
    factory = "verknode.mining.DBSCAN"
    name = "DBSCAN"
    category = CLUSTER_CATEGORY
    node_type = "Learner"
    icon = "DBS"
    short_description = "Density-based clustering (DBSCAN)."
    description = ("Clusters dense regions: points with at least <i>MinPts</i> neighbours within <i>Epsilon</i> form "
                   "clusters (cluster_0, ...); points not reachable from any cluster are labeled <code>Noise</code>.")
    in_ports = [Port(TABLE, "Input data")]
    out_ports = [Port(TABLE, "Clustered data", "Input data with Cluster column")]
    fields = [F("eps", "float", "Epsilon", 0.5), F("min_points", "int", "Minimum points (MinPts)", 5),
              F("distance", "enum", "Distance", "Euclidean", options=list(DISTANCES))] + _cluster_fields()

    def configure(self, in_specs, s, ctx):
        _numeric_features(s, in_specs[0])
        if s["eps"] <= 0 or s["min_points"] < 1:
            raise InvalidSettingsError("Epsilon must be positive and MinPts at least 1")
        return [pl.Schema({**in_specs[0], "Cluster": pl.String})]

    def execute(self, inputs, s, ctx):
        from sklearn.cluster import DBSCAN as SKDBSCAN

        df = inputs[0]
        feats = _numeric_features(s, df.schema)
        fm, X, skipped = _prep_only(df, feats, s["missing"])
        est = SKDBSCAN(eps=s["eps"], min_samples=s["min_points"], metric=DISTANCES[s["distance"]]).fit(X)
        _, valid = fm.matrix(df)
        labels = scatter(est.labels_, valid, df.height, pl.Int64).to_list()
        n = len(set(est.labels_) - {-1})
        ctx.log(f"Found {n} clusters, {int((est.labels_ < 0).sum())} noise points")
        return [df.with_columns(pl.Series("Cluster", _cluster_names(labels), dtype=pl.String))]


LINKAGES = {"Single": "single", "Complete": "complete", "Average": "average", "Ward": "ward"}


class HierarchicalClustering(NodeModel):
    factory = "verknode.mining.HierarchicalClustering"
    name = "Hierarchical Clustering"
    category = CLUSTER_CATEGORY
    node_type = "Learner"
    icon = "HC"
    has_view = True
    short_description = "Agglomerative hierarchical clustering with dendrogram."
    description = ("Builds a cluster hierarchy bottom-up with the chosen linkage and cuts it into the given number "
                   "of clusters. The view shows the dendrogram.")
    in_ports = [Port(TABLE, "Input data")]
    out_ports = [Port(TABLE, "Clustered data", "Input data with Cluster column")]
    fields = [F("n_clusters", "int", "Number output clusters", 3),
              F("linkage", "enum", "Linkage type", "Average", options=list(LINKAGES)),
              F("distance", "enum", "Distance function", "Euclidean", options=list(DISTANCES))] + _cluster_fields()

    def _validate(self, s):
        if s["n_clusters"] < 1:
            raise InvalidSettingsError("Number output clusters must be at least 1")
        if s["linkage"] == "Ward" and s["distance"] != "Euclidean":
            raise InvalidSettingsError("Ward linkage requires the Euclidean distance")

    def configure(self, in_specs, s, ctx):
        _numeric_features(s, in_specs[0])
        self._validate(s)
        return [pl.Schema({**in_specs[0], "Cluster": pl.String})]

    def execute(self, inputs, s, ctx):
        from scipy.cluster.hierarchy import dendrogram, fcluster, linkage

        self._validate(s)
        df = inputs[0]
        feats = _numeric_features(s, df.schema)
        fm, X, skipped = _prep_only(df, feats, s["missing"])
        if X.shape[0] < 2:
            raise ValueError("At least two rows are required")
        Z = linkage(X.to_numpy(), method=LINKAGES[s["linkage"]], metric=DISTANCES[s["distance"]])
        labels = fcluster(Z, t=s["n_clusters"], criterion="maxclust") - 1
        _, valid = fm.matrix(df)
        full = scatter(labels, valid, df.height, pl.Int64).to_list()
        fig, ax = views.figure(8, 4.5)
        dendrogram(Z, ax=ax, truncate_mode="lastp" if X.shape[0] > 40 else None, p=40, color_threshold=None)
        ax.set_ylabel("Distance")
        ctx.set_view(views.page("Hierarchical Clustering - Dendrogram", views.chart(views.svg(fig))))
        return [df.with_columns(pl.Series("Cluster", _cluster_names(full), dtype=pl.String))]


# ----------------------------------------------------------------------
# PCA
# ----------------------------------------------------------------------
PCA_CATEGORY = "Analytics/Mining/Dimension Reduction/PCA"
DIM_MODES = ["Dimensions to reduce to", "Minimum information fraction (%)"]


def _pca_fit(df, feats, mode, standardize):
    from sklearn.decomposition import PCA as SKPCA

    fm, X, _, skipped = fit_model(df, None, feats, mode, SKPCA(), scale=standardize)
    if X.shape[0] < 2:
        raise ValueError("At least two rows are required")
    return fm, X, skipped


def _pca_dims(s, cumulative, n_features):
    if s["dim_mode"] == DIM_MODES[0]:
        d = s["dimensions"]
        if not 1 <= d <= n_features:
            raise InvalidSettingsError(f"Number of dimensions must be between 1 and {n_features}")
        return d
    if cumulative is None:
        return None
    target = s["min_information"] / 100.0
    if not 0 < target <= 1:
        raise InvalidSettingsError("Minimum information fraction must be in (0, 100]")
    return int(np.searchsorted(np.asarray(cumulative) - 1e-12, target) + 1)


def _pca_apply(fm, df, dims, remove):
    X, valid = fm.matrix(df)
    proj = fm.pipeline.transform(X) if X.shape[0] else np.zeros((0, dims))
    cols = [scatter(proj[:, i], valid, df.height, pl.Float64).alias(f"PCA dimension {i}") for i in range(dims)]
    out = df.drop(fm.numeric + fm.nominal) if remove else df
    return add_columns(out, cols)


def _pca_schema(schema, feats, dims, remove):
    if dims is None:
        return None
    base = {c: t for c, t in schema.items() if not (remove and c in feats)}
    return pl.Schema({**base, **{f"PCA dimension {i}": pl.Float64 for i in range(dims)}})


PCA_DIM_FIELDS = [
    F("dim_mode", "enum", "Target dimensionality", DIM_MODES[0], options=DIM_MODES),
    F("dimensions", "int", "Dimensions to reduce to", 2, show_if={"dim_mode": DIM_MODES[0]}),
    F("min_information", "float", "Minimum information fraction (%)", 90.0, show_if={"dim_mode": DIM_MODES[1]}),
    F("remove", "bool", "Remove original data columns", False),
]
PCA_FIT_FIELDS = [
    F("features", "columns", "Columns", dict(ALL_COLUMNS), "Numeric columns to reduce."),
    F("standardize", "bool", "Standardize (z-score) columns", False, "Use the correlation instead of the covariance matrix."),
    F("missing", "enum", "Missing values", MISSING_SKIP, options=MISSING_OPTIONS),
]


class PCACompute(NodeModel):
    factory = "verknode.mining.PCACompute"
    name = "PCA Compute"
    category = PCA_CATEGORY
    node_type = "Learner"
    icon = "PCΣ"
    has_view = True
    short_description = "Computes the principal components of the input data."
    description = ("Computes the covariance matrix and its spectral decomposition (eigenvalues and principal "
                   "axes). The model is applied with <i>PCA Apply</i>.")
    in_ports = [Port(TABLE, "Input data")]
    out_ports = [Port(TABLE, "Covariance matrix"), Port(TABLE, "Spectral decomposition", "Eigenvalues and principal axes"),
                 Port(MODEL, "Transformation model", "PCA model for PCA Apply")]
    fields = PCA_FIT_FIELDS

    def configure(self, in_specs, s, ctx):
        schema = in_specs[0]
        feats = _numeric_features(s, schema)
        return [pl.Schema({"Column": pl.String, **{c: pl.Float64 for c in feats}}),
                pl.Schema({"Component": pl.String, "eigenvalue": pl.Float64, **{c: pl.Float64 for c in feats}}),
                ModelPortObject("PCA", None, pl.Schema({c: schema[c] for c in feats}), None, None, None, {"Task": "PCA"})]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        feats = _numeric_features(s, df.schema)
        fm, X, skipped = _pca_fit(df, feats, s["missing"], s["standardize"])
        pca = fm.estimator
        cov = np.atleast_2d(np.cov(fm.transformed(X), rowvar=False))
        cov_table = pl.DataFrame({"Column": pl.Series(feats, dtype=pl.String),
                                  **{c: pl.Series(cov[:, j], dtype=pl.Float64) for j, c in enumerate(feats)}})
        k = len(pca.explained_variance_)
        spectral = pl.DataFrame({
            "Component": pl.Series([f"principal component {i}" for i in range(k)], dtype=pl.String),
            "eigenvalue": pl.Series(pca.explained_variance_, dtype=pl.Float64),
            **{c: pl.Series(pca.components_[:, j], dtype=pl.Float64) for j, c in enumerate(feats)}})
        cumulative = [round(float(v), 6) for v in np.cumsum(pca.explained_variance_ratio_)]
        info = {"Task": "PCA", "Cumulative explained variance": cumulative}
        model = ModelPortObject("PCA", fm, pl.Schema({c: df.schema[c] for c in feats}), None, None, None, info)
        if skipped:
            ctx.warn(f"{skipped} row(s) with missing values were not used")
        fig, ax = views.figure(6, 3.5)
        ax.bar(range(k), pca.explained_variance_ratio_, color=views.PALETTE[0], label="per component")
        ax.plot(range(k), cumulative, color=views.PALETTE[1], marker="o", label="cumulative")
        ax.set_xlabel("Principal component")
        ax.set_ylabel("Explained variance ratio")
        ax.legend()
        ctx.set_view(views.page("PCA Compute", views.chart(views.svg(fig)), views.table(spectral)))
        return [cov_table, spectral, model]


class PCAApply(NodeModel):
    factory = "verknode.mining.PCAApply"
    name = "PCA Apply"
    category = PCA_CATEGORY
    node_type = "Predictor"
    icon = "PC▸"
    short_description = "Projects data onto principal components of a PCA model."
    description = "Projects the input rows onto the first principal components (columns <code>PCA dimension 0</code>, ...)."
    in_ports = [Port(MODEL, "Transformation model"), Port(TABLE, "Table to transform")]
    out_ports = [Port(TABLE, "Transformed data")]
    fields = PCA_DIM_FIELDS

    def configure(self, in_specs, s, ctx):
        model, schema = in_specs
        check_model(model, ("PCA",))
        check_features(model, schema)
        dims = _pca_dims(s, model.info.get("Cumulative explained variance"), len(model.features))
        return [_pca_schema(schema, model.features, dims, s["remove"])]

    def execute(self, inputs, s, ctx):
        model, df = inputs
        check_model(model, ("PCA",))
        check_features(model, df)
        fm = model.estimator
        dims = _pca_dims(s, model.info.get("Cumulative explained variance"), len(model.features))
        return [_pca_apply(fm, df, dims, s["remove"])]


class PCA(NodeModel):
    factory = "verknode.mining.PCA"
    name = "PCA"
    category = PCA_CATEGORY
    node_type = "Manipulator"
    icon = "PCA"
    short_description = "Principal component analysis (compute and apply)."
    description = "Reduces the selected numeric columns to their principal components in one step."
    in_ports = [Port(TABLE, "Input data")]
    out_ports = [Port(TABLE, "Transformed data")]
    fields = PCA_FIT_FIELDS + PCA_DIM_FIELDS

    def configure(self, in_specs, s, ctx):
        schema = in_specs[0]
        feats = _numeric_features(s, schema)
        return [_pca_schema(schema, feats, _pca_dims(s, None, len(feats)), s["remove"])]

    def execute(self, inputs, s, ctx):
        df = inputs[0]
        feats = _numeric_features(s, df.schema)
        fm, X, skipped = _pca_fit(df, feats, s["missing"], s["standardize"])
        cumulative = list(np.cumsum(fm.estimator.explained_variance_ratio_))
        dims = _pca_dims(s, cumulative, len(feats))
        if skipped:
            ctx.warn(f"{skipped} row(s) with missing values get missing projections")
        ctx.log(f"Reduced {len(feats)} columns to {dims} dimensions "
                f"({cumulative[dims - 1] * 100:.1f}% of the variance)")
        return [_pca_apply(fm, df, dims, s["remove"])]
