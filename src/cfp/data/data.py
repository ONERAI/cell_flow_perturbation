import abc
import warnings
import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import anndata
import jax
import jax.numpy as jnp
import numpy as np
import pandas as pd
import scipy.sparse as sp
import sklearn.preprocessing as preprocessing
from tqdm import tqdm

from cfp._constants import CONTROL_HELPER
from cfp._types import ArrayLike

from .utils import _to_list, _flatten_list

__all__ = ["TrainingData", "ValidationData"]


@dataclass
class BaseData(abc.ABC):
    """Base class for data containers."""

    cell_data: jax.Array | None = None  # (n_cells, n_features)
    src_data: dict[int, jnp.ndarray] | None = None
    tgt_data: dict[int, dict[int, jnp.ndarray]] | None = None
    condition_data: dict[str | int, jnp.ndarray] | None = None
    split_covariates_mask: jax.Array | None = (
        None  # (n_cells,), which cell assigned to which source distribution
    )
    split_idx_to_covariates: dict[int, str] | None = None
    perturbation_covariates_mask: jax.Array | None = (
        None  # (n_cells,), which cell assigned to which target distribution
    )
    perturbation_idx_to_covariates: dict[int, tuple[str, ...]] | None = None
    control_to_perturbation: dict[int, jax.Array] | None = None
    max_combination_length: int | None = None
    null_value: Any | None = None
    null_token: Any | None = None

    @classmethod
    @abc.abstractmethod
    def load_from_adata(cls, adata: anndata.AnnData, **kwargs) -> "BaseData":
        """Load data from an AnnData object.

        Args:
            adata: An :class:`~anndata.AnnData` object.

        Returns
        -------
            BaseData: Data container.
        """
        pass


class PerturbationData(BaseData):
    """Base class for perturbation data containers."""

    @staticmethod
    def _get_cell_data(
        adata: anndata.AnnData, sample_rep: Literal["X"] | dict[str, str]
    ) -> jax.Array:
        error_message = "`sample_rep` should be either `X`, a key in `adata.obsm` or a dictionary of the form {`attr`: `key`}."
        if sample_rep == "X":
            sample_rep = adata.X
            if isinstance(sample_rep, sp.csr_matrix):
                return jnp.asarray(sample_rep.toarray())
            else:
                return jnp.asarray(sample_rep)
        if isinstance(sample_rep, str):
            if sample_rep not in adata.obsm:
                raise ValueError(error_message)
            return jnp.asarray(adata.obsm[sample_rep])
        if not isinstance(sample_rep, dict):
            raise ValueError(error_message)
        attr = list(sample_rep.keys())[0]
        key = list(sample_rep.values())[0]
        return jnp.asarray(getattr(adata, attr)[key])

    @staticmethod
    def _verify_control_data(adata: anndata.AnnData, key: tuple[str, Any]):
        if key not in adata.obs:
            raise ValueError(f"Control column '{key}' not found in adata.obs.")
        if not isinstance(adata.obs[key].dtype, pd.BooleanDtype):
            try:
                adata.obs[key] = adata.obs[key].astype("boolean")
            except ValueError as e:
                raise ValueError(
                    f"Control column '{key}' could not be converted to boolean."
                ) from e
        if adata.obs[key].sum() == 0:
            raise ValueError(f"No control cells found in adata.")

    @classmethod
    def _verify_perturbation_covariates(cls, adata: anndata.AnnData, data: Any) -> None:
        if not isinstance(data, dict):
            raise ValueError(
                f"`perturbation_covariates` should be a dictionary, found {data} to be of type {type(data)}."
            )
        if len(data) == 0:
            raise ValueError("No perturbation covariates provided.")
        for key, covars in data.items():
            if not isinstance(key, str):
                raise ValueError(
                    f"Key should be a string, found {key} to be of type {type(key)}."
                )
            if not isinstance(covars, tuple | list):
                raise ValueError(
                    f"Value should be a tuple, found {covar} to be of type {type(covar)}."
                )
            if len(covars) == 0:
                raise ValueError(
                    f"No covariates provided for perturbation group {key}."
                )
            cls._verify_covariate_data(adata, covars)

    @classmethod
    def _verify_sample_covariates(cls, adata: anndata.AnnData, data: Any) -> None:
        if not isinstance(data, tuple | list):
            raise ValueError(
                f"`sample_covariates` should be a tuple or list, found {data} to be of type {type(data)}."
            )
        for covar in data:
            if not isinstance(covar, str):
                raise ValueError(
                    f"Key should be a string, found {covar} to be of type {type(covar)}."
                )
            cls._verify_covariate_data(adata, _to_list(covar))

    @classmethod
    def _verify_split_covariates(cls, adata: anndata.AnnData, data: Any) -> None:
        if not isinstance(data, tuple | list):
            raise ValueError(
                f"`split_covariates` should be a tuple or list, found {data} to be of type {type(data)}."
            )
        for covar in data:
            if not isinstance(covar, str):
                raise ValueError(
                    f"Key should be a string, found {covar} to be of type {type(covar)}."
                )
            cls._verify_covariate_data(adata, _to_list(covar))

    @staticmethod
    def _verify_covariate_data(adata: anndata.AnnData, covars: Any) -> None:
        for covariate in covars:
            if covariate is not None and covariate not in adata.obs:
                raise ValueError(f"Covariate {covariate} not found in adata.obs.")

    @classmethod
    def _get_linked_covariates(
        cls, adata: anndata.AnnData, perturb_covariates: dict[str, Sequence[str]]
    ) -> dict[str, Sequence[str]]:
        cls._verify_linked_covars(perturb_covariates)

        primary_group, primary_covars = list(perturb_covariates.items())[0]
        primary_to_linked = {k: {} for k in primary_covars}
        for cov_group, covars in list(perturb_covariates.items())[1:]:
            for primary_cov, linked_cov in zip(primary_covars, covars):
                primary_to_linked[primary_cov][cov_group] = linked_cov

        return primary_to_linked

    @staticmethod
    def _verify_linked_covars(perturb_covariates: dict[str, str]) -> None:
        lengths = [len(covs) for covs in perturb_covariates.values()]
        if len(set(lengths)) != 1:
            raise ValueError(
                f"Length of perturbation covariate groups must match, found {lengths}."
            )

    @staticmethod
    def _verify_covariate_reps(
        adata: anndata.AnnData, data: dict[str, str], covariates: Sequence[str]
    ) -> None:
        for key, value in data.items():
            if key not in covariates:
                raise ValueError(f"Key '{key}' not found in covariates.")
            if value not in adata.uns:
                raise ValueError(f"Representation '{value}' not found in `adata.uns`.")
            if not isinstance(adata.uns[value], dict):
                raise ValueError(
                    f"Covariate representation '{value}' in `adata.uns` should be of type `dict`, found {type(adata.uns[value])}."
                )

    @staticmethod
    def _get_max_combination_length(
        perturbation_covariates: dict[str, Sequence[str]],
        max_combination_length: int | None,
    ) -> int:
        obs_max_combination_length = max(
            len(comb) for comb in perturbation_covariates.values()
        )
        if max_combination_length is None:
            return obs_max_combination_length
        elif max_combination_length < obs_max_combination_length:
            warnings.warn(
                f"Provided `max_combination_length` is smaller than the observed maximum combination length of the perturbation covariates. Setting maximum combination length to {obs_max_combination_length}."
            )
            return obs_max_combination_length
        else:
            return max_combination_length

    @classmethod
    def _get_primary_covar_encoder(
        cls,
        adata: anndata.AnnData,
        perturbation_covariates: dict[str, Sequence[str]],
        perturbation_covariate_reps: dict[str, str],
    ) -> preprocessing.OneHotEncoder:
        primary_group, primary_covars = list(perturbation_covariates.items())[0]
        is_categorical = cls._check_covariate_type(adata, primary_covars)

        if primary_group in perturbation_covariate_reps:
            return None

        if is_categorical:
            encoder = preprocessing.OneHotEncoder(sparse_output=False)
            all_values = np.unique(adata.obs[primary_covars].values.flatten())
            encoder.fit(all_values.reshape(-1, 1))
            return encoder

        encoder = preprocessing.OneHotEncoder(sparse_output=False)
        encoder.fit(np.array(primary_covars).reshape(-1, 1))
        return encoder, is_categorical

    @staticmethod
    def _check_covariate_type(adata: anndata.AnnData, covars: dict[str, str]) -> bool:
        col_is_cat = []
        for covariate in covars:
            try:
                adata.obs[covariate] = adata.obs[covariate].astype(float)
            except ValueError:
                try:
                    adata.obs[covariate] = adata.obs[covariate].astype("category")
                    col_is_cat.append(True)
                except ValueError:
                    raise ValueError(
                        f"Perturbation covariates `{covariate}` should be either numeric/boolean or categorical."
                    ) from e
            else:
                col_is_cat.append(False)

        if max(col_is_cat) != min(col_is_cat):
            raise ValueError(
                f"Groups of perturbation covariates `{covariate}` should be either all numeric/boolean or all categorical."
            )

        return max(col_is_cat)

    @staticmethod
    def _check_shape(arr: float | ArrayLike) -> ArrayLike:
        if not hasattr(arr, "shape") or len(arr.shape) == 0:
            return jnp.ones((1, 1)) * arr
        if arr.ndim == 1:  # type: ignore[union-attr]
            return jnp.expand_dims(arr, 0)  # type: ignore[return-value]
        elif arr.ndim == 2:  # type: ignore[union-attr]
            if arr.shape[0] == 1:
                return arr  # type: ignore[return-value]
            if arr.shape[1] == 1:
                return jnp.transpose(arr)
            raise ValueError(
                "Condition representation has an unexpected shape. Should be (1, n_features) or (n_features, )."
            )
        elif arr.ndim > 2:  # type: ignore[union-attr]
            raise ValueError(
                "Condition representation has too many dimensions. Should be 1 or 2."
            )

        raise ValueError(
            "Condition representation as an unexpected format. Expected an array of shape (1, n_features) or (n_features, )."
        )

    @staticmethod
    def _get_idx_to_covariate(
        covariate_groups: dict[str, Sequence[str]]
    ) -> dict[int, str]:
        idx_to_covar = {}
        for idx, cov_group in enumerate(covariate_groups):
            idx_to_covar[idx] = cov_group
        covar_to_idx = {v: k for k, v in idx_to_covar.items()}
        return idx_to_covar, covar_to_idx

    @staticmethod
    def _pad_to_max_length(
        arr: jax.Array, max_combination_length: int, null_value: Any
    ) -> jax.Array:
        if arr.shape[0] < max_combination_length:
            null_arr = jnp.full(
                (max_combination_length - arr.shape[0], arr.shape[1]), null_value
            )
            arr = jnp.concatenate([arr, null_arr], axis=0)
        return arr

    @classmethod
    def _get_perturbation_covariates(
        cls,
        condition_data: pd.DataFrame,
        rep_dict: dict[str, dict[str, ArrayLike]],
        perturb_covariates: dict[str, Sequence[str]],
        sample_covariates: dict[str, Sequence[str]],
        covariate_reps: dict[str, str],
        primary_to_linked: dict[str, Sequence[str]],
        primary_encoder: preprocessing.OneHotEncoder,
        max_combination_length: int,
        null_value: Any = 0.0,
    ) -> dict[int, jax.Array]:
        embeddings = {}

        primary_group, primary_covars = list(perturb_covariates.items())[0]
        primary_encoder, primary_is_cat = primary_encoder

        perturb_covar_emb = {group: [] for group in perturb_covariates}
        for primary_cov in primary_covars:
            value = condition_data[primary_cov]

            cov_name = value if primary_is_cat else primary_cov

            if primary_group in covariate_reps:
                if cov_name not in rep_dict[primary_group]:
                    raise ValueError(
                        f"Representation for '{cov_name}' not found in `adata.uns['{primary_group}']`."
                    )
                prim_arr = jnp.asarray(rep_dict[primary_group][cov_name])
            else:
                prim_arr = jnp.asarray(
                    primary_encoder.transform(np.array(cov_name).reshape(-1, 1))
                )

            if not primary_is_cat:
                prim_arr *= value

            prim_arr = cls._check_shape(prim_arr)
            perturb_covar_emb[primary_group].append(prim_arr)

            for linked_covar in primary_to_linked[primary_cov].items():
                linked_group, linked_cov = list(linked_covar)

                if linked_cov is None:
                    linked_arr = jnp.full((1, 1), null_value)
                    linked_arr = cls._check_shape(linked_arr)
                    perturb_covar_emb[linked_group].append(linked_arr)
                    continue

                cov_name = condition_data[linked_cov]

                if linked_group in covariate_reps:
                    if cov_name not in rep_dict[linked_group]:
                        raise ValueError(
                            f"Representation for '{cov_name}' not found in `adata.uns['{linked_group}']`."
                        )
                    linked_arr = jnp.asarray(rep_dict[linked_group][cov_name])
                else:
                    linked_arr = jnp.asarray(condition_data[linked_cov])

                linked_arr = cls._check_shape(linked_arr)
                perturb_covar_emb[linked_group].append(linked_arr)

        perturb_covar_emb = {
            k: cls._pad_to_max_length(
                jnp.concatenate(v, axis=0), max_combination_length, null_value
            )
            for k, v in perturb_covar_emb.items()
        }

        sample_covar_emb = {}
        for sample_cov in sample_covariates:
            if sample_cov in covariate_reps:
                cov_arr = jnp.asarray(
                    rep_dict[sample_cov][condition_data[sample_cov].values[0]]
                )
            else:
                cov_arr = jnp.asarray(condition_data[sample_cov])

            cov_arr = cls._check_shape(cov_arr)
            sample_covar_emb[sample_cov] = jnp.tile(
                cov_arr, (max_combination_length, 1)
            )

        return perturb_covar_emb | sample_covar_emb


class TrainingData(PerturbationData):
    """Data container for the perturbation data.

    Parameters
    ----------
    cell_data
        The representation of cell data, e.g. PCA of gene expression data.
    split_covariates_mask
        Mask of the split covariates.
    split_idx_to_covariates
        Dictionary explaining values in split_covariates_mask.
    perturbation_covariates_mask
        Mask of the perturbation covariates.
    perturbation_idx_to_covariates
        Dictionary explaining values in perturbation_covariates_mask.
    condition_data
        Dictionary with embeddings for conditions.
    control_to_perturbation
        Mapping from control index to target distribution indices.
    max_combination_length
        Maximum number of covariates in a combination.
    null_value
        Values in :attr:`anndata.AnnData.obs` columns which indicate no treatment with the corresponding covariate. These values will be masked with `null_token`.
    null_token
        Token to use for masking `null_value`.
    """

    cell_data: jax.Array  # (n_cells, n_features)
    split_covariates_mask: (
        jax.Array
    )  # (n_cells,), which cell assigned to which source distribution
    split_idx_to_covariates: dict[
        int, str
    ]  # (n_sources,) dictionary explaining split_covariates_mask
    perturbation_covariates_mask: (
        jax.Array
    )  # (n_cells,), which cell assigned to which target distribution
    perturbation_idx_to_covariates: dict[
        int, tuple[str, ...]
    ]  # (n_targets,), dictionary explaining perturbation_covariates_mask
    condition_data: (
        dict[str, jnp.ndarray] | None
    )  # (n_targets,) all embeddings for conditions
    control_to_perturbation: dict[
        int, jax.Array
    ]  # mapping from control idx to target distribution idcs
    max_combination_length: int
    null_value: Any
    null_token: Any

    @classmethod
    def load_from_adata(
        cls,
        adata: anndata.AnnData,
        sample_rep: str,
        control_key: str,
        perturbation_covariates: dict[str, Sequence[str]],
        perturbation_covariate_reps: dict[str, str] | None = None,
        sample_covariates: Sequence[str] | None = None,
        sample_covariate_reps: dict[str, str] | None = None,
        split_covariates: Sequence[str] | None = None,
        max_combination_length: int | None = None,
        null_value: float = 0.0,
    ) -> "TrainingData":
        """Load cell data from an AnnData object.

        Args:
            adata: An :class:`~anndata.AnnData` object.
            sample_rep: Key in `adata.obsm` where the sample representation is stored or "X" to use `adata.X`.
            control_key: Key of a boolean column in `adata.obs` that defines the control samples.
            perturbation_covariates: A dictionary where the keys indicate the name of the covariate group and the values are keys in `adata.obs`. The corresponding columns should be either boolean (presence/abscence of the perturbation) or numeric (concentration or magnitude of the perturbation). If multiple groups are provided, the first is interpreted as the primary perturbation and the others as covariates corresponding to these perturbations, e.g. `{"drug":("drugA", "drugB"), "time":("drugA_time", "drugB_time")}`.
            perturbation_covariate_reps: A dictionary where the keys indicate the name of the covariate group and the values are keys in `adata.uns` storing a dictionary with the representation of the covariates. E.g. `{"drug":"drug_embeddings"}` with `adata.uns["drug_embeddings"] = {"drugA": np.array, "drugB": np.array}`.
            sample_covariates: Keys in `adata.obs` indicating sample covatiates to be taken into account for training and prediction, e.g. `["age", "cell_type"]`.
            sample_covariate_reps: A dictionary where the keys indicate the name of the covariate group and the values are keys in `adata.uns` storing a dictionary with the representation of the covariates. E.g. `{"cell_type": "cell_type_embeddings"}` with `adata.uns["cell_type_embeddings"] = {"cell_typeA": np.array, "cell_typeB": np.array}`.
            split_covariates: Covariates in adata.obs to split all control cells into different control populations. The perturbed cells are also split according to these columns, but if these covariates should also be encoded in the model, the corresponding column should also be used in `perturbation_covariates` or `sample_covariates`.
            max_combination_length: Maximum number of combinations of primary `perturbation_covariates`. If `None`, the value is inferred from the provided `perturbation_covariates`.
            null_value: Value to use for padding to `max_combination_length`.

        Returns
        -------
            TraingingData: Data container for the perturbation data.
        """
        # TODO: add device to possibly only load to cpu
        cls._verify_control_data(adata, control_key)
        perturbation_covariates = {
            k: _to_list(v) for k, v in perturbation_covariates.items()
        }

        cls._verify_perturbation_covariates(adata, perturbation_covariates)

        primary_to_linked = cls._get_linked_covariates(adata, perturbation_covariates)

        sample_covariates = sample_covariates or []
        cls._verify_sample_covariates(adata, sample_covariates)

        perturbation_covariate_reps = perturbation_covariate_reps or {}
        sample_covariate_reps = sample_covariate_reps or {}

        sample_cov_groups = {covar: _to_list(covar) for covar in sample_covariates}
        covariate_groups = perturbation_covariates | sample_cov_groups
        covariate_reps = perturbation_covariate_reps | sample_covariate_reps
        cls._verify_covariate_reps(adata, covariate_reps, covariate_groups)

        split_covariates = split_covariates or []
        cls._verify_split_covariates(adata, split_covariates)

        max_combination_length = cls._get_max_combination_length(
            perturbation_covariates, max_combination_length
        )

        idx_to_covar, covar_to_idx = cls._get_idx_to_covariate(covariate_groups)

        primary_encoder = cls._get_primary_covar_encoder(
            adata, perturbation_covariates, perturbation_covariate_reps
        )

        src_counter = 0
        tgt_counter = 0

        if len(split_covariates) > 0:
            split_cov_combs = adata.obs[split_covariates].drop_duplicates().values
        else:
            split_cov_combs = [[]]

        perturb_covar_keys = _flatten_list(perturbation_covariates.values()) + list(
            sample_covariates
        )
        perturb_covar_keys = [k for k in perturb_covar_keys if k is not None]
        perturb_covar_df = adata.obs[perturb_covar_keys].drop_duplicates().reset_index()

        control_to_perturbation: dict[int, int] = {}
        split_covariates_mask = np.full((adata.n_obs,), -1, dtype=jnp.int32)
        split_idx_to_covariates = {}
        perturbation_covariates_mask = np.full((adata.n_obs,), -1, dtype=jnp.int32)
        perturbation_idx_to_covariates = {}
        condition_data: dict[int, list] = {i: [] for i in covar_to_idx.keys()}

        for split_combination in split_cov_combs:
            filter_dict = dict(zip(split_covariates, split_combination, strict=False))
            split_cov_mask = (
                adata.obs[list(filter_dict.keys())] == list(filter_dict.values())
            ).all(axis=1)
            split_covariates_mask[split_cov_mask] = src_counter
            split_idx_to_covariates[src_counter] = split_combination
            src_counter += 1

            conditional_distributions = []

            pbar = tqdm(perturb_covar_df.iterrows(), total=perturb_covar_df.shape[0])
            for _, tgt_cond in pbar:
                tgt_cond = tgt_cond[perturb_covar_keys]
                mask = (
                    (adata.obs[perturb_covar_keys] == list(tgt_cond.values)).all(axis=1)
                    * split_cov_mask
                ) == 1

                if mask.sum() == 0:
                    continue

                conditional_distributions.append(tgt_counter)
                perturbation_covariates_mask[mask] = tgt_counter
                perturbation_idx_to_covariates[tgt_counter] = tgt_cond.values

                embedding = cls._get_perturbation_covariates(
                    condition_data=tgt_cond,
                    rep_dict=adata.uns,
                    perturb_covariates=perturbation_covariates,
                    sample_covariates=sample_covariates,
                    covariate_reps=covariate_reps,
                    primary_to_linked=primary_to_linked,
                    primary_encoder=primary_encoder,
                    max_combination_length=max_combination_length,
                    null_value=null_value,
                )

                for pert_cov, emb in embedding.items():
                    condition_data[pert_cov].append(emb)

                tgt_counter += 1

            control_to_perturbation[src_counter] = np.array(conditional_distributions)
            src_counter += 1

        for pert_cov, emb in condition_data.items():
            condition_data[pert_cov] = jnp.array(emb)

        return cls(
            cell_data=cls._get_cell_data(adata, sample_rep),
            split_covariates_mask=jnp.asarray(split_covariates_mask),
            split_idx_to_covariates=split_idx_to_covariates,
            perturbation_covariates_mask=jnp.asarray(perturbation_covariates_mask),
            perturbation_idx_to_covariates=perturbation_idx_to_covariates,
            condition_data=condition_data,
            control_to_perturbation=control_to_perturbation,
            max_combination_length=max_combination_length,
            null_value=null_value,
        )

    @property
    def n_controls(self) -> int:
        """Returns the number of control covariate values."""
        return len(self.split_idx_to_covariates)

    @property
    def n_perturbations(self) -> int:
        """Returns the number of perturbation covariate combinations."""
        return len(self.perturbation_idx_to_covariates)

    @property
    def n_perturbation_covariates(self) -> int:
        """Returns the number of perturbation covariates."""
        return len(self.condition_data)

    def _format_params(self, fmt: Callable[[Any], str]) -> str:
        params = {
            "n_controls": self.n_controls,
            "n_perturbations": self.n_perturbations,
        }
        return ", ".join(f"{name}={fmt(val)}" for name, val in params.items())

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}[{self._format_params(repr)}]"


class ValidationData(PerturbationData):
    """Data container for the validation data.

    Parameters
    ----------
    src_data
        Dictionary with data for source cells.
    tgt_data
        Dictionary with data for target cells.
    condition_data
        Dictionary with embeddings for conditions.
    max_combination_length
        Maximum number of covariates in a combination.
    null_value
        Values in :attr:`anndata.AnnData.obs` columns which indicate no treatment with the corresponding covariate. These values will be masked with `null_token`.
    null_token
        Token to use for masking `null_value`.
    """

    src_data: dict[int, jnp.ndarray]
    tgt_data: dict[int, dict[int, jnp.ndarray]]
    condition_data: dict[int, jnp.ndarray] | None
    max_combination_length: int
    null_value: Any
    null_token: Any

    @classmethod
    def load_from_adata(
        cls,
        adata: anndata.AnnData,
        sample_rep: Literal["X"] | dict[str, str],
        control_data: Sequence[str, Any],
        split_covariates: Sequence[str],
        obs_perturbation_covariates: Sequence[tuple[str, ...]],
        uns_perturbation_covariates: Sequence[dict[str, tuple[str, ...] | str]],
        max_combination_length: int,
        # TODO: Having null_value nan can cause problems with matching conditions,
        # expecially if there are both float and str columns.
        null_value: Any = None,
        null_token: Any = 0.0,
    ) -> "ValidationData":
        """Load cell data from an AnnData object.

        Args:
            adata: An :class:`~anndata.AnnData` object.
            cell_data: Where to read the cell data from. Either 'X', a key in adata.obsm or a dictionary of the form {attr: key}, where 'attr' is an attribute of the :class:`~anndata.AnnData` object and key is the 'key' in the corresponding key.
            control_data: Tuple of length 2 with first element defining the column in :class:`~anndata.AnnData` and second element defining the value in `adata.obs[control_data[0]]` used to define all control cells.
            split_covariates: Covariates in adata.obs to split all control cells into different control populations. The perturbed cells are also split according to these columns, but if an embedding for these covariates should be encoded in the model, the corresponding column should also be used in `obs_perturbation_covariates` or `uns_perturbation_covariates`.
            obs_perturbation_covariates: Tuples of covariates in adata.obs characterizing the perturbed cells (together with `split_covariates` and `uns_perturbation_covariates`) and encoded by the values as found in `adata.obs`. If a tuple contains more than
            one element, this is interpreted as a combination of covariates that should be treated as an unordered set.
            uns_perturbation_covariates: Dictionaries with keys in adata.uns and values columns in adata.obs which characterize the perturbed cells (together with `split_covariates` and `obs_perturbation_covariates`) and encoded by the values as found in `adata.uns[uns_perturbation_covariates.keys()]`. If a value of the dictionary is a tuple with more than one element, this is interpreted as a combination of covariates that should be treated as an unordered set.
            max_combination_length: Maximum number of covariates in a combination.
            null_value: Values in :attr:`anndata.AnnData.obs` columns which indicate no treatment with the corresponding covariate. These values will be masked with `null_token`.
            null_token: Token to use for masking `null_value`.

        Returns
        -------
            PerturbationData: Data container for the perturbation data.
        """
        # TODO(@MUCDK): add device to possibly only load to cpu
        if split_covariates is None or len(split_covariates) == 0:
            adata.obs[CONTROL_HELPER] = True
            adata.obs[CONTROL_HELPER] = adata.obs[CONTROL_HELPER].astype("category")
            split_covariates = [CONTROL_HELPER]

        # Check if control cells are present in the data
        cls._verify_control_data(adata, control_data)
        obs_perturbation_covariates = [_to_list(c) for c in obs_perturbation_covariates]
        cls._verify_obs_perturbation_covariates(adata, obs_perturbation_covariates)
        uns_perturbation_covariates = {
            k: _to_list(v) for k, v in uns_perturbation_covariates.items()
        }
        cls._verify_uns_perturbation_covariates(adata, uns_perturbation_covariates)

        obs_combination_length = (
            max(len(comb) for comb in obs_perturbation_covariates)
            if len(obs_perturbation_covariates)
            else 0
        )
        uns_combination_length = (
            max(len(comb) for comb in uns_perturbation_covariates.values())  # type: ignore[attr-defined]
            if len(uns_perturbation_covariates)
            else 0
        )

        observed_combination_length = max(
            obs_combination_length, uns_combination_length
        )
        if observed_combination_length > max_combination_length:
            raise ValueError(
                f"Observed combination length of the validation data({observed_combination_length}) is larger than the provided maximum combination length of the training data ({max_combination_length})."
            )

        for covariate in split_covariates:
            if covariate not in adata.obs:
                raise ValueError(f"Split covariate {covariate} not found in adata.obs.")
            if adata.obs[covariate].dtype.name != "category":
                adata.obs[covariate] = adata.obs[covariate].astype("category")

        pert_embedding_idx_to_covariates = cls._get_idx_to_covariate(
            obs_perturbation_covariates, uns_perturbation_covariates
        )
        pert_embedding_idx_to_covariates_reversed = {
            v: k for k, v in pert_embedding_idx_to_covariates.items()
        }

        src_dist = {
            covariate: adata.obs[covariate].cat.categories
            for covariate in split_covariates
        }
        tgt_dist_obs = {
            covariate: adata.obs[covariate].cat.categories
            for group in obs_perturbation_covariates
            for covariate in group
        }
        tgt_dist_uns = {
            covariate: adata.obs[covariate].cat.categories
            for emb_covariates in uns_perturbation_covariates.values()  # type: ignore[attr-defined]
            for covariate in emb_covariates
        }
        tgt_dist_obs.update(tgt_dist_uns)

        src_data: dict[int, jax.Array] = {}
        tgt_data: dict[int, dict[int, jax.Array]] = {}
        condition_data: dict[int, dict[int, list]] | None = (
            None
            if (
                len(obs_perturbation_covariates) == 0
                and len(uns_perturbation_covariates) == 0
            )
            else {}
        )

        src_counter = 0
        src_dists = list(itertools.product(*src_dist.values()))
        observed_tgt_combs = (
            adata.obs[list(tgt_dist_obs.keys())].drop_duplicates().values
        )
        control_mask = (adata.obs[control_data[0]] == control_data[1]) == 1
        for src_combination in src_dists:
            filter_dict = dict(zip(split_covariates, src_combination, strict=False))
            split_cov_mask = (
                adata.obs[list(filter_dict.keys())] == list(filter_dict.values())
            ).all(axis=1)
            mask = split_cov_mask * control_mask
            if mask.sum() == 0:
                continue

            src_data[src_counter] = cls._get_sample_data(adata[mask], cell_data)
            tgt_data[src_counter] = {}
            condition_data[src_counter] = {}

            tgt_counter = 0
            for tgt_combination in tqdm(observed_tgt_combs):
                mask = (
                    (adata.obs[list(tgt_dist_obs.keys())] == list(tgt_combination)).all(
                        axis=1
                    )
                    * (1 - control_mask)
                    * split_cov_mask
                ) == 1
                if mask.sum() == 0:
                    continue

                tgt_data[src_counter][tgt_counter] = cls._get_sample_data(
                    adata[mask], cell_data
                )

                if condition_data is not None:
                    embedding = cls._get_perturbation_covariates(
                        adata=adata[mask],
                        embedding_dict=adata.uns,
                        obs_perturbation_covariates=obs_perturbation_covariates,
                        uns_perturbation_covariates=uns_perturbation_covariates,
                        max_combination_length=max_combination_length,
                        pert_embedding_idx_to_covariates_reversed=pert_embedding_idx_to_covariates_reversed,
                        null_value=null_value,
                        null_token=null_token,
                    )

                    condition_data[src_counter][tgt_counter] = {}
                    for pert_cov, emb in embedding.items():
                        pert_key = pert_embedding_idx_to_covariates[pert_cov]
                        condition_data[src_counter][tgt_counter][pert_key] = (
                            jnp.expand_dims(emb, 0)
                        )

                tgt_counter += 1
            src_counter += 1

        return cls(
            tgt_data=tgt_data,
            src_data=src_data,
            condition_data=condition_data,
            max_combination_length=max_combination_length,
            null_value=null_value,
            null_token=null_token,
        )
