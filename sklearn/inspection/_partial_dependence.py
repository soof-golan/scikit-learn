"""Partial dependence plots for regression and classification models."""

# Authors: Peter Prettenhofer
#          Trevor Stephens
#          Nicolas Hug
# License: BSD 3 clause

from collections.abc import Iterable

import numpy as np
from scipy import sparse
from scipy.stats.mstats import mquantiles

from ._pd_utils import _check_feature_names, _get_feature_index
from ..base import is_classifier, is_regressor
from ..utils.extmath import cartesian
from ..utils import check_array
from ..utils import check_matplotlib_support  # noqa
from ..utils import _safe_indexing
from ..utils import _safe_assign
from ..utils import _determine_key_type
from ..utils import _get_column_indices
from ..utils.validation import check_is_fitted
from ..utils import Bunch
from ..tree import DecisionTreeRegressor
from ..ensemble import RandomForestRegressor
from ..exceptions import NotFittedError
from ..ensemble._gb import BaseGradientBoosting
from ..ensemble._hist_gradient_boosting.gradient_boosting import (
    BaseHistGradientBoosting,
)


__all__ = [
    "partial_dependence",
]


def _grid_from_X(X, percentiles, is_categorical, grid_resolution):
    """Generate a grid of points based on the percentiles of X.

    The grid is a cartesian product between the columns of ``values``. The
    ith column of ``values`` consists in ``grid_resolution`` equally-spaced
    points between the percentiles of the jth column of X.

    If ``grid_resolution`` is bigger than the number of unique values in the
    j-th column of X or if the feature is a categorical feature (by inspecting
    `is_categorical`) , then those unique values will be used instead.

    Parameters
    ----------
    X : array-like of shape (n_samples, n_target_features)
        The data.

    percentiles : tuple of float
        The percentiles which are used to construct the extreme values of
        the grid. Must be in [0, 1].

    is_categorical : list of bool
        For each feature, tells whether it is categorical or not. If a feature
        is categorical, then the values used will be the unique ones
        (i.e. categories) instead of the percentiles.

    grid_resolution : int
        The number of equally spaced points to be placed on the grid for each
        feature.

    Returns
    -------
    grid : ndarray of shape (n_points, n_target_features)
        A value for each feature at each point in the grid. ``n_points`` is
        always ``<= grid_resolution ** X.shape[1]``.

    values : list of 1d ndarrays
        The values with which the grid has been created. The size of each
        array ``values[j]`` is either ``grid_resolution``, or the number of
        unique values in ``X[:, j]``, whichever is smaller.
    """
    if not isinstance(percentiles, Iterable) or len(percentiles) != 2:
        raise ValueError("'percentiles' must be a sequence of 2 elements.")
    if not all(0 <= x <= 1 for x in percentiles):
        raise ValueError("'percentiles' values must be in [0, 1].")
    if percentiles[0] >= percentiles[1]:
        raise ValueError("percentiles[0] must be strictly less than percentiles[1].")

    if grid_resolution <= 1:
        raise ValueError("'grid_resolution' must be strictly greater than 1.")

    values = []
    for feature, is_cat in enumerate(is_categorical):
        uniques = np.unique(_safe_indexing(X, feature, axis=1))
        if is_cat or uniques.shape[0] < grid_resolution:
            # Use the unique values either because:
            # - feature has low resolution use unique values
            # - feature is categorical
            axis = uniques
        else:
            # create axis based on percentiles and grid resolution
            emp_percentiles = mquantiles(
                _safe_indexing(X, feature, axis=1), prob=percentiles, axis=0
            )
            if np.allclose(emp_percentiles[0], emp_percentiles[1]):
                raise ValueError(
                    "percentiles are too close to each other, "
                    "unable to build the grid. Please choose percentiles "
                    "that are further apart."
                )
            axis = np.linspace(
                emp_percentiles[0],
                emp_percentiles[1],
                num=grid_resolution,
                endpoint=True,
            )
        values.append(axis)

    return cartesian(values), values


def _partial_dependence_recursion(est, grid, features):
    averaged_predictions = est._compute_partial_dependence_recursion(grid, features)
    if averaged_predictions.ndim == 1:
        # reshape to (1, n_points) for consistency with
        # _partial_dependence_brute
        averaged_predictions = averaged_predictions.reshape(1, -1)

    return averaged_predictions


def _partial_dependence_brute(est, grid, features, X, response_method):

    predictions = []
    averaged_predictions = []

    # define the prediction_method (predict, predict_proba, decision_function).
    if is_regressor(est):
        prediction_method = est.predict
    else:
        predict_proba = getattr(est, "predict_proba", None)
        decision_function = getattr(est, "decision_function", None)
        if response_method == "auto":
            # try predict_proba, then decision_function if it doesn't exist
            prediction_method = predict_proba or decision_function
        else:
            prediction_method = (
                predict_proba
                if response_method == "predict_proba"
                else decision_function
            )
        if prediction_method is None:
            if response_method == "auto":
                raise ValueError(
                    "The estimator has no predict_proba and no "
                    "decision_function method."
                )
            elif response_method == "predict_proba":
                raise ValueError("The estimator has no predict_proba method.")
            else:
                raise ValueError("The estimator has no decision_function method.")

    X_eval = X.copy()
    for new_values in grid:
        for i, variable in enumerate(features):
            _safe_assign(X_eval, new_values[i], column_indexer=variable)

        try:
            # Note: predictions is of shape
            # (n_points,) for non-multioutput regressors
            # (n_points, n_tasks) for multioutput regressors
            # (n_points, 1) for the regressors in cross_decomposition (I think)
            # (n_points, 2) for binary classification
            # (n_points, n_classes) for multiclass classification
            pred = prediction_method(X_eval)

            predictions.append(pred)
            # average over samples
            averaged_predictions.append(np.mean(pred, axis=0))
        except NotFittedError as e:
            raise ValueError("'estimator' parameter must be a fitted estimator") from e

    n_samples = X.shape[0]

    # reshape to (n_targets, n_instances, n_points) where n_targets is:
    # - 1 for non-multioutput regression and binary classification (shape is
    #   already correct in those cases)
    # - n_tasks for multi-output regression
    # - n_classes for multiclass classification.
    predictions = np.array(predictions).T
    if is_regressor(est) and predictions.ndim == 2:
        # non-multioutput regression, shape is (n_instances, n_points,)
        predictions = predictions.reshape(n_samples, -1)
    elif is_classifier(est) and predictions.shape[0] == 2:
        # Binary classification, shape is (2, n_instances, n_points).
        # we output the effect of **positive** class
        predictions = predictions[1]
        predictions = predictions.reshape(n_samples, -1)

    # reshape averaged_predictions to (n_targets, n_points) where n_targets is:
    # - 1 for non-multioutput regression and binary classification (shape is
    #   already correct in those cases)
    # - n_tasks for multi-output regression
    # - n_classes for multiclass classification.
    averaged_predictions = np.array(averaged_predictions).T
    if is_regressor(est) and averaged_predictions.ndim == 1:
        # non-multioutput regression, shape is (n_points,)
        averaged_predictions = averaged_predictions.reshape(1, -1)
    elif is_classifier(est) and averaged_predictions.shape[0] == 2:
        # Binary classification, shape is (2, n_points).
        # we output the effect of **positive** class
        averaged_predictions = averaged_predictions[1]
        averaged_predictions = averaged_predictions.reshape(1, -1)

    return averaged_predictions, predictions


def partial_dependence(
    estimator,
    X,
    features,
    *,
    categorical_features=None,
    feature_names=None,
    response_method="auto",
    percentiles=(0.05, 0.95),
    grid_resolution=100,
    method="auto",
    kind="average",
):
    """Partial dependence of ``features``.

    Partial dependence of a feature (or a set of features) corresponds to
    the average response of an estimator for each possible value of the
    feature.

    Read more in the :ref:`User Guide <partial_dependence>`.

    .. warning::

        For :class:`~sklearn.ensemble.GradientBoostingClassifier` and
        :class:`~sklearn.ensemble.GradientBoostingRegressor`, the
        `'recursion'` method (used by default) will not account for the `init`
        predictor of the boosting process. In practice, this will produce
        the same values as `'brute'` up to a constant offset in the target
        response, provided that `init` is a constant estimator (which is the
        default). However, if `init` is not a constant estimator, the
        partial dependence values are incorrect for `'recursion'` because the
        offset will be sample-dependent. It is preferable to use the `'brute'`
        method. Note that this only applies to
        :class:`~sklearn.ensemble.GradientBoostingClassifier` and
        :class:`~sklearn.ensemble.GradientBoostingRegressor`, not to
        :class:`~sklearn.ensemble.HistGradientBoostingClassifier` and
        :class:`~sklearn.ensemble.HistGradientBoostingRegressor`.

    Parameters
    ----------
    estimator : BaseEstimator
        A fitted estimator object implementing :term:`predict`,
        :term:`predict_proba`, or :term:`decision_function`.
        Multioutput-multiclass classifiers are not supported.

    X : {array-like or dataframe} of shape (n_samples, n_features)
        ``X`` is used to generate a grid of values for the target
        ``features`` (where the partial dependence will be evaluated), and
        also to generate values for the complement features when the
        `method` is 'brute'.

    features : array-like of {int, str}
        The feature (e.g. `[0]`) or pair of interacting features
        (e.g. `[(0, 1)]`) for which the partial dependency should be computed.

    categorical_features : array-like of shape (n_features,) or shape \
            (n_categorical_features,), dtype={bool, int, str}, default=None
        Indicates the categorical features.

        - `None`: no feature will be considered categorical;
        - boolean array-like: boolean mask of shape `(n_features,)`
            indicating which features are categorical. Thus, this array has
            the same shape has `X.shape[1]`;
        - integer or string array-like: integer indices or strings
            indicating categorical features.

        .. versionadded:: 1.2

    feature_names : array-like of shape (n_features,), dtype=str, default=None
        Name of each feature; `feature_names[i]` holds the name of the feature
        with index `i`.
        By default, the name of the feature corresponds to their numerical
        index for NumPy array and their column name for pandas dataframe.

        .. versionadded:: 1.2

    response_method : {'auto', 'predict_proba', 'decision_function'}, \
            default='auto'
        Specifies whether to use :term:`predict_proba` or
        :term:`decision_function` as the target response. For regressors
        this parameter is ignored and the response is always the output of
        :term:`predict`. By default, :term:`predict_proba` is tried first
        and we revert to :term:`decision_function` if it doesn't exist. If
        ``method`` is 'recursion', the response is always the output of
        :term:`decision_function`.

    percentiles : tuple of float, default=(0.05, 0.95)
        The lower and upper percentile used to create the extreme values
        for the grid. Must be in [0, 1].

    grid_resolution : int, default=100
        The number of equally spaced points on the grid, for each target
        feature.

    method : {'auto', 'recursion', 'brute'}, default='auto'
        The method used to calculate the averaged predictions:

        - `'recursion'` is only supported for some tree-based estimators
          (namely
          :class:`~sklearn.ensemble.GradientBoostingClassifier`,
          :class:`~sklearn.ensemble.GradientBoostingRegressor`,
          :class:`~sklearn.ensemble.HistGradientBoostingClassifier`,
          :class:`~sklearn.ensemble.HistGradientBoostingRegressor`,
          :class:`~sklearn.tree.DecisionTreeRegressor`,
          :class:`~sklearn.ensemble.RandomForestRegressor`,
          ) when `kind='average'`.
          This is more efficient in terms of speed.
          With this method, the target response of a
          classifier is always the decision function, not the predicted
          probabilities. Since the `'recursion'` method implicitly computes
          the average of the Individual Conditional Expectation (ICE) by
          design, it is not compatible with ICE and thus `kind` must be
          `'average'`.

        - `'brute'` is supported for any estimator, but is more
          computationally intensive.

        - `'auto'`: the `'recursion'` is used for estimators that support it,
          and `'brute'` is used otherwise.

        Please see :ref:`this note <pdp_method_differences>` for
        differences between the `'brute'` and `'recursion'` method.

    kind : {'average', 'individual', 'both'}, default='average'
        Whether to return the partial dependence averaged across all the
        samples in the dataset or one value per sample or both.
        See Returns below.

        Note that the fast `method='recursion'` option is only available for
        `kind='average'`. Computing individual dependencies requires using the
        slower `method='brute'` option.

        .. versionadded:: 0.24

    Returns
    -------
    predictions : :class:`~sklearn.utils.Bunch`
        Dictionary-like object, with the following attributes.

        individual : ndarray of shape (n_outputs, n_instances, \
                len(values[0]), len(values[1]), ...)
            The predictions for all the points in the grid for all
            samples in X. This is also known as Individual
            Conditional Expectation (ICE)

        average : ndarray of shape (n_outputs, len(values[0]), \
                len(values[1]), ...)
            The predictions for all the points in the grid, averaged
            over all samples in X (or over the training data if
            ``method`` is 'recursion').
            Only available when ``kind='both'``.

        values : seq of 1d ndarrays
            The values with which the grid has been created.

            .. deprecated:: 1.3
                The key `values` has been deprecated in 1.3 and will be removed
                in 1.5 in favor of `pdp_values`. See `pdp_values` for details
                about the `values` attribute.

        pdp_values : seq of 1d ndarrays
            The values with which the grid has been created. The generated
            grid is a cartesian product of the arrays in ``pdp_values``.
            ``len(pdp_values) == len(features)``. The size of each array
            ``pdp_values[j]`` is either ``grid_resolution``, or the number of
            unique values in ``X[:, j]``, whichever is smaller.

            .. versionadded:: 1.3

        ``n_outputs`` corresponds to the number of classes in a multi-class
        setting, or to the number of tasks for multi-output regression.
        For classical regression and binary classification ``n_outputs==1``.
        ``n_values_feature_j`` corresponds to the size ``pdp_values[j]``.

    See Also
    --------
    PartialDependenceDisplay.from_estimator : Plot Partial Dependence.
    PartialDependenceDisplay : Partial Dependence visualization.

    Examples
    --------
    >>> X = [[0, 0, 2], [1, 0, 0]]
    >>> y = [0, 1]
    >>> from sklearn.ensemble import GradientBoostingClassifier
    >>> gb = GradientBoostingClassifier(random_state=0).fit(X, y)
    >>> partial_dependence(gb, features=[0], X=X, percentiles=(0, 1),
    ...                    grid_resolution=2) # doctest: +SKIP
    (array([[-4.52...,  4.52...]]), [array([ 0.,  1.])])
    """
    check_is_fitted(estimator)

    if not (is_classifier(estimator) or is_regressor(estimator)):
        raise ValueError("'estimator' must be a fitted regressor or classifier.")

    if is_classifier(estimator) and isinstance(estimator.classes_[0], np.ndarray):
        raise ValueError("Multiclass-multioutput estimators are not supported")

    # Use check_array only on lists and other non-array-likes / sparse. Do not
    # convert DataFrame into a NumPy array.
    if not (hasattr(X, "__array__") or sparse.issparse(X)):
        X = check_array(X, force_all_finite="allow-nan", dtype=object)

    accepted_responses = ("auto", "predict_proba", "decision_function")
    if response_method not in accepted_responses:
        raise ValueError(
            "response_method {} is invalid. Accepted response_method names "
            "are {}.".format(response_method, ", ".join(accepted_responses))
        )

    if is_regressor(estimator) and response_method != "auto":
        raise ValueError(
            "The response_method parameter is ignored for regressors and "
            "must be 'auto'."
        )

    accepted_methods = ("brute", "recursion", "auto")
    if method not in accepted_methods:
        raise ValueError(
            "method {} is invalid. Accepted method names are {}.".format(
                method, ", ".join(accepted_methods)
            )
        )

    if kind != "average":
        if method == "recursion":
            raise ValueError(
                "The 'recursion' method only applies when 'kind' is set to 'average'"
            )
        method = "brute"

    if method == "auto":
        if isinstance(estimator, BaseGradientBoosting) and estimator.init is None:
            method = "recursion"
        elif isinstance(
            estimator,
            (BaseHistGradientBoosting, DecisionTreeRegressor, RandomForestRegressor),
        ):
            method = "recursion"
        else:
            method = "brute"

    if method == "recursion":
        if not isinstance(
            estimator,
            (
                BaseGradientBoosting,
                BaseHistGradientBoosting,
                DecisionTreeRegressor,
                RandomForestRegressor,
            ),
        ):
            supported_classes_recursion = (
                "GradientBoostingClassifier",
                "GradientBoostingRegressor",
                "HistGradientBoostingClassifier",
                "HistGradientBoostingRegressor",
                "HistGradientBoostingRegressor",
                "DecisionTreeRegressor",
                "RandomForestRegressor",
            )
            raise ValueError(
                "Only the following estimators support the 'recursion' "
                "method: {}. Try using method='brute'.".format(
                    ", ".join(supported_classes_recursion)
                )
            )
        if response_method == "auto":
            response_method = "decision_function"

        if response_method != "decision_function":
            raise ValueError(
                "With the 'recursion' method, the response_method must be "
                "'decision_function'. Got {}.".format(response_method)
            )

    if _determine_key_type(features, accept_slice=False) == "int":
        # _get_column_indices() supports negative indexing. Here, we limit
        # the indexing to be positive. The upper bound will be checked
        # by _get_column_indices()
        if np.any(np.less(features, 0)):
            raise ValueError("all features must be in [0, {}]".format(X.shape[1] - 1))

    features_indices = np.asarray(
        _get_column_indices(X, features), dtype=np.int32, order="C"
    ).ravel()

    feature_names = _check_feature_names(X, feature_names)

    n_features = X.shape[1]
    if categorical_features is None:
        is_categorical = [False] * len(features_indices)
    else:
        categorical_features = np.array(categorical_features, copy=False)
        if categorical_features.dtype.kind == "b":
            # categorical features provided as a list of boolean
            if categorical_features.size != n_features:
                raise ValueError(
                    "When `categorical_features` is a boolean array-like, "
                    "the array should be of shape (n_features,). Got "
                    f"{categorical_features.size} elements while `X` contains "
                    f"{n_features} features."
                )
            is_categorical = [categorical_features[idx] for idx in features_indices]
        elif categorical_features.dtype.kind in ("i", "O", "U"):
            # categorical features provided as a list of indices or feature names
            categorical_features_idx = [
                _get_feature_index(cat, feature_names=feature_names)
                for cat in categorical_features
            ]
            is_categorical = [
                idx in categorical_features_idx for idx in features_indices
            ]
        else:
            raise ValueError(
                "Expected `categorical_features` to be an array-like of boolean,"
                f" integer, or string. Got {categorical_features.dtype} instead."
            )

    grid, values = _grid_from_X(
        _safe_indexing(X, features_indices, axis=1),
        percentiles,
        is_categorical,
        grid_resolution,
    )

    if method == "brute":
        averaged_predictions, predictions = _partial_dependence_brute(
            estimator, grid, features_indices, X, response_method
        )

        # reshape predictions to
        # (n_outputs, n_instances, n_values_feature_0, n_values_feature_1, ...)
        predictions = predictions.reshape(
            -1, X.shape[0], *[val.shape[0] for val in values]
        )
    else:
        averaged_predictions = _partial_dependence_recursion(
            estimator, grid, features_indices
        )

    # reshape averaged_predictions to
    # (n_outputs, n_values_feature_0, n_values_feature_1, ...)
    averaged_predictions = averaged_predictions.reshape(
        -1, *[val.shape[0] for val in values]
    )
    pdp_results = Bunch()

    msg = (
        "Key: 'values', is deprecated in 1.3 and will be removed in 1.5. "
        "Please use 'pdp_values' instead."
    )
    pdp_results._set_deprecated(
        values, new_key="pdp_values", deprecated_key="values", warning_message=msg
    )

    if kind == "average":
        pdp_results["average"] = averaged_predictions
    elif kind == "individual":
        pdp_results["individual"] = predictions
    else:  # kind='both'
        pdp_results["average"] = averaged_predictions
        pdp_results["individual"] = predictions

    return pdp_results
