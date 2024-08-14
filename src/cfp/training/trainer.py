from collections.abc import Callable, Sequence
from typing import Any, Literal

import jax
import numpy as np
from numpy.typing import ArrayLike
from tqdm import tqdm

from cfp.data.dataloader import TrainSampler, ValidationSampler
from cfp.solvers import _genot, _otfm
from cfp.training.callbacks import CallbackRunner


class CellFlowTrainer:
    """Trainer for the OTFM/GENOT model with a conditional velocity field.

    Args:
        dataloader: Data sampler.
        model: OTFM/GENOT model with a conditional velocity field.
        seed: Random seed for subsampling validation data.

    Returns
    -------
        None
    """

    def __init__(
        self,
        model: _otfm.OTFlowMatching | _genot.GENOT,
        seed: int = 0,
    ):
        if not isinstance(model, (_otfm.OTFlowMatching | _genot.GENOT)):
            raise NotImplementedError(
                f"Model must be an instance of OTFlowMatching or GENOT, got {type(model)}"
            )

        self.model = model
        self.rng_subsampling = np.random.default_rng(seed)
        self.training_logs: dict[str, Any] = {}

    def _validation_step(
        self,
        val_data: dict[str, dict[str, dict[str, ArrayLike]]],
        mode: Literal["on_log_iteration", "on_train_end"] = "on_log_iteration",
    ) -> dict[str, dict[str, dict[str, ArrayLike]]]:
        """Compute predictions for validation data."""
        # TODO: Sample fixed number of conditions to validate on

        valid_pred_data: dict[str, dict[str, ArrayLike]] = {}
        valid_true_data: dict[str, dict[str, ArrayLike]] = {}
        for val_key, vdl in val_data.items():
            batch = vdl.sample(mode=mode)
            src = batch["source"]
            condition = batch.get("condition", None)
            true_tgt = batch["target"]
            valid_pred_data[val_key] = jax.tree.map(self.model.predict, src, condition)
            valid_true_data[val_key] = true_tgt

        return valid_true_data, valid_pred_data

    def _update_logs(self, logs: dict[str, Any]) -> None:
        """Update training logs."""
        for k, v in logs.items():
            if k not in self.training_logs:
                self.training_logs[k] = []
            self.training_logs[k].append(v)

    def train(
        self,
        dataloader: TrainSampler,
        num_iterations: int,
        valid_freq: int,
        valid_loaders: dict[str, ValidationSampler] | None = None,
        monitor_metrics: Sequence[str] = [],
        callbacks: Sequence[Callable] = [],
    ) -> None:
        """Trains the model.

        Args:
            num_iterations: Number of iterations to train the model.
            batch_size: Batch size.
            valid_freq: Frequency of validation.
            callbacks: Callback functions.
            monitor_metrics: Metrics to monitor.

        Returns
        -------
            None
        """
        self.training_logs = {"loss": []}
        rng = jax.random.PRNGKey(0)

        # Initiate callbacks
        valid_loaders = valid_loaders or {}
        crun = CallbackRunner(
            callbacks=callbacks,
        )
        crun.on_train_begin()

        pbar = tqdm(range(num_iterations))
        for it in pbar:
            rng, rng_step_fn = jax.random.split(rng, 2)
            batch = dataloader.sample(rng)
            loss = self.model.step_fn(rng_step_fn, batch)
            self.training_logs["loss"].append(float(loss))

            if ((it - 1) % valid_freq == 0) and (it > 1):
                # Get predictions from validation data
                valid_true_data, valid_pred_data = self._validation_step(
                    valid_loaders, mode="on_log_iteration"
                )

                # Run callbacks
                metrics = crun.on_log_iteration(valid_true_data, valid_pred_data)
                self._update_logs(metrics)

                # Update progress bar
                mean_loss = np.mean(self.training_logs["loss"][-valid_freq:])
                postfix_dict = {
                    metric: round(self.training_logs[metric][-1], 3)
                    for metric in monitor_metrics
                }
                postfix_dict["loss"] = round(mean_loss, 3)
                pbar.set_postfix(postfix_dict)

        if num_iterations > 0:
            valid_true_data, valid_pred_data = self._validation_step(
                valid_loaders, mode="on_train_end"
            )
            metrics = crun.on_train_end(valid_true_data, valid_pred_data)
            self._update_logs(metrics)
