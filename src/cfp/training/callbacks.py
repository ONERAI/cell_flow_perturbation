import abc
from typing import Any, Literal

import jax.tree as jt
import numpy as np
import jax
from numpy.typing import ArrayLike

from cfp.data.data import ValidationData
from cfp.metrics.metrics import compute_e_distance, compute_r_squared, compute_scalar_mmd, compute_sinkhorn_div


class BaseCallback(abc.ABC):
    """Base class for callbacks in the CellFlowTrainer"""

    @abc.abstractmethod
    def on_train_begin(self, *args: Any, **kwargs: Any) -> None:
        """Called at the beginning of training"""
        pass

    @abc.abstractmethod
    def on_log_iteration(self, *args: Any, **kwargs: Any) -> Any:
        """Called at each validation/log iteration"""
        pass

    @abc.abstractmethod
    def on_train_end(self, *args: Any, **kwargs: Any) -> Any:
        """Called at the end of training"""
        pass


class LoggingCallback(BaseCallback, abc.ABC):
    """Base class for logging callbacks in the CellFlowTrainer"""

    @abc.abstractmethod
    def on_train_begin(self) -> Any:
        """Called at the beginning of training to initiate logging"""
        pass

    @abc.abstractmethod
    def on_log_iteration(self, dict_to_log: dict[str, Any]) -> Any:
        """Called at each validation/log iteration to log data

        Args:
            dict_to_log: Dictionary containing data to log
        """
        pass

    @abc.abstractmethod
    def on_train_end(self, dict_to_log: dict[str, Any]) -> Any:
        """Called at the end of trainging to log data

        Args:
            dict_to_log: Dictionary containing data to log
        """
        pass


class ComputationCallback(BaseCallback, abc.ABC):
    """Base class for computation callbacks in the CellFlowTrainer"""

    @abc.abstractmethod
    def on_train_begin(self) -> Any:
        """Called at the beginning of training to initiate metric computation"""
        pass

    @abc.abstractmethod
    def on_log_iteration(
        self,
        validation_data: dict[str, ValidationData],
        predicted_data: dict[str, dict[str, ArrayLike]],
        training_data: dict[str, ArrayLike],
    ) -> dict[str, float]:
        """Called at each validation/log iteration to compute metrics

        Args:
            validation_data: Validation data
            predicted_data: Predicted data
            training_data: Current batch and predicted data
        """
        pass

    @abc.abstractmethod
    def on_train_end(
        self,
        validation_data: dict[str, ValidationData],
        predicted_data: dict[str, dict[str, ArrayLike]],
        training_data: dict[str, ArrayLike],
    ) -> dict[str, float]:
        """Called at the end of training to compute metrics

        Args:
            validation_data: Validation data
            predicted_data: Predicted data
            training_data: Current batch and predicted data
        """
        pass


metric_to_func = {
    "r_squared": compute_r_squared,
    "mmd": compute_scalar_mmd,
    "sinkhorn_div": compute_sinkhorn_div,
    "e_distance": compute_e_distance,
}


class ComputeMetrics(ComputationCallback):
    """Callback to compute metrics on validation data during training

    Parameters
    ----------
    metrics : list
        List of metrics to compute
    metric_aggregation : list
        List of aggregation functions to use for each metric

    Returns
    -------
        None
    """

    def __init__(
        self,
        metrics: list[Literal["r_squared", "mmd", "sinkhorn_div", "e_distance"]],
        metric_aggregation: list[Literal["mean", "median"]] = "mean",
    ):
        self.metrics = metrics
        self.metric_aggregation = metric_aggregation
        self._aggregation_func = (
            np.median if metric_aggregation == "median" else np.mean
        )
        for metric in metrics:
            # TODO: support custom callables as metrics
            if metric not in metric_to_func:
                raise ValueError(
                    f"Metric {metric} not supported. Supported metrics are {list(metric_to_func.keys())}"
                )

    def on_train_begin(self, *args: Any, **kwargs: Any) -> Any:
        pass

    def on_log_iteration(
        self,
        validation_data: dict[str, ValidationData],
        predicted_data: dict[str, dict[str, ArrayLike]],
        training_data: dict[str, ArrayLike],
    ) -> dict[str, float]:
        """Called at each validation/log iteration to compute metrics

        Args:
            validation_data: Validation data
            predicted_data: Predicted data
            training_data: Current batch and predicted data
        """
        metrics = {}
        for metric in self.metrics:
            for k in validation_data.keys():
                result = jt.flatten(
                    jt.map(
                        metric_to_func[metric],
                        validation_data[k].tgt_data,
                        predicted_data[k],
                    )
                )[0]
                # TODO: support multiple aggregation functions
                metrics[f"{k}_{metric}"] = self._aggregation_func(result)
            result = metric_to_func[metric](
                training_data["tgt_cell_data"], training_data["pred_data"]
            )
            metrics[f"train_{metric}"] = self._aggregation_func(result)

        return metrics

    def on_train_end(
        self,
        validation_data: dict[str, ValidationData],
        predicted_data: dict[str, dict[str, ArrayLike]],
        training_data: dict[str, ArrayLike],
    ) -> dict[str, float]:
        """Called at the end of training to compute metrics

        Args:
            validation_data: Validation data
            predicted_data: Predicted data
            training_data: Current batch and predicted data
        """
        return self.on_log_iteration(validation_data, predicted_data)


class WandbLogger(LoggingCallback):
    """Callback to log data to Weights and Biases

    Parameters
    ----------
    project : str
        The project name in wandb
    out_dir : str
        The output directory to save the logs
    config : dict
        The configuration to log
    **kwargs : Any
        Additional keyword arguments to pass to wandb.init

    Returns
    -------
        None
    """

    try:
        import wandb
    except ImportError:
        raise ImportError(
            "wandb is not installed, please install it via `pip install wandb`"
        )
    try:
        import omegaconf
    except ImportError:
        raise ImportError(
            "omegaconf is not installed, please install it via `pip install omegaconf`"
        )

    def __init__(
        self,
        project: str,
        out_dir: str,
        config: omegaconf.OmegaConf | dict[str, Any],
        **kwargs,
    ):
        self.project = project
        self.out_dir = out_dir
        self.config = config
        self.kwargs = kwargs

    def on_train_begin(self) -> Any:
        """Called at the beginning of training to initiate WandB logging"""
        if isinstance(self.config, dict):
            config = omegaconf.OmegaConf.create(self.config)
        wandb.login()
        wandb.init(
            project=wandb_project,
            config=omegaconf.OmegaConf.to_container(config, resolve=True),
            dir=out_dir,
            settings=wandb.Settings(
                start_method=self.kwargs.pop("start_method", "thread")
            ),
        )

    def on_log_iteration(
        self,
        dict_to_log: dict[str, float],
        **_: Any,
    ) -> Any:
        """Called at each validation/log iteration to log data to WandB"""
        wandb.log(dict_to_log)

    def on_train_end(self, dict_to_log: dict[str, float]) -> Any:
        """Called at the end of training to log data to WandB"""
        wandb.log(dict_to_log)


class CallbackRunner:
    """Runs a set of computational and logging callbacks in the CellFlowTrainer

    Args:
        computation_callbacks: List of computation callbacks
        logging_callbacks: List of logging callbacks
        data: Validation data to use for computing metrics
        seed: Random seed for subsampling the validation data

    Returns
    -------
        None
    """

    def __init__(
        self,
        callbacks: list[ComputationCallback],
        data: dict[str, ValidationData],
        seed: int = 0,
    ) -> None:

        self.validation_data = data
        self.computation_callbacks = [
            c for c in callbacks if isinstance(c, ComputationCallback)
        ]
        self.logging_callbacks = [
            c for c in callbacks if isinstance(c, LoggingCallback)
        ]
        self.rng = np.random.default_rng(seed)

        if len(self.computation_callbacks) == 0 & len(self.logging_callbacks) != 0:
            raise ValueError(
                "No computation callbacks defined to compute metrics to log"
            )

    def _sample_validation_data(
        self, stage: Literal["on_log_iteration", "on_train_end"]
    ) -> dict[str, ValidationData]:
        """Sample validation data for computing metrics"""
        if stage == "on_train_end":
            n_conditions_to_sample = lambda x: x.n_conditions_on_train_end
        elif stage == "on_log_iteration":
            n_conditions_to_sample = lambda x: x.n_conditions_on_log_iteration
        else:
            raise ValueError(f"Stage {stage} not supported.")
        subsampled_validation_data = {}
        for val_data_name, val_data in self.validation_data.items():
            if n_conditions_to_sample(val_data) == -1:
                subsampled_validation_data[val_data_name] = val_data
            else:
                idxs = self.rng.choice(
                    len(val_data.condition_data), n_conditions_to_sample(val_data), replace=False
                )
                subsampled_validation_data[val_data_name] = {
                    val_data_name[i]: val_data[val_data_name[i]] for i in idxs
                }
        return subsampled_validation_data

    def on_train_begin(self) -> Any:
        """Called at the beginning of training to initiate callbacks"""
        for callback in self.computation_callbacks:
            callback.on_train_begin()

        for callback in self.logging_callbacks:
            callback.on_train_begin()

    def on_log_iteration(self, train_data, pred_data) -> dict[str, Any]:
        """Called at each validation/log iteration to run callbacks. First computes metrics with computation callbacks and then logs data with logging callbacks.

        Args:
            train_data: Training data
            pred_data: Predicted data

        Returns
        -------
            dict_to_log: Dictionary containing data to log
        """
        validation_data = self._sample_validation_data(stage="on_log_iteration")
        dict_to_log: dict[str, Any] = {}

        for callback in self.computation_callbacks:
            results = callback.on_log_iteration(validation_data, pred_data, train_data)
            dict_to_log.update(results)

        for callback in self.logging_callbacks:
            callback.on_log_iteration(dict_to_log)

        return dict_to_log

    def on_train_end(self, train_data, pred_data) -> dict[str, Any]:
        """Called at the end of training to run callbacks. First computes metrics with computation callbacks and then logs data with logging callbacks.

        Args:
            train_data: Training data
            pred_data: Predicted data

        Returns
        -------
            dict_to_log: Dictionary containing data to log
        """
        dict_to_log: dict[str, Any] = {}
        validation_data = self._sample_validation_data(stage="on_log_iteration")

        for callback in self.computation_callbacks:
            results = callback.on_log_iteration(validation_data, pred_data, train_data)
            dict_to_log.update(results)

        for callback in self.logging_callbacks:
            callback.on_log_iteration(dict_to_log)

        return dict_to_log
