"""LSTM 深度学习模型（DS-6 的第 4 类模型，CPU 训练）。

口径与边界：

- **每行一个时间步**：输入形状为 ``(batch, 1, features)``。本模块**不**构造跨段滑窗序列；
  序列历史已经以滞后/累计列的形式存在于特征矩阵里，滑窗会重新引入跨段借位风险（HS-D-7）。
- **单层 + 小隐层**：``hidden_size=32``、``num_layers=1``。可用样本只有约 1.6~2.0 千条、
  测试段更少，容量越大越容易过拟合，结论也越不可信（HS-D-12）。
- **CPU 确定性**：``torch.manual_seed`` + 由种子派生的 ``torch.Generator`` 洗牌 +
  ``torch.use_deterministic_algorithms(True)``；训练结束后**恢复**该全局开关，
  不把副作用留给同进程的其它测试。
- **早停只用验证段**：监控验证段 MSE，``patience=20``，``epochs<=300``；验证段无样本时
  退化为监控训练段 MSE。

缺失处理与线性模型一致：含 ``NaN`` 特征的行先剔除并计数上报，标准化的均值/标准差同样
只由训练段拟合。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from numbers import Integral, Real

import numpy as np
import torch
from torch import nn

from .splitting import SegmentSamples, Standardizer, as_float_matrix

DEFAULT_HIDDEN_SIZE = 32
DEFAULT_EPOCHS = 300
DEFAULT_PATIENCE = 20
DEFAULT_BATCH_SIZE = 64
DEFAULT_LEARNING_RATE = 0.01
DEFAULT_LAYERS = 1

#: 训练设备固定为 CPU：本机 GPU 可用性不影响可复现性，也不与其它任务争抢显存。
DEVICE = "cpu"


class _LstmRegressor(nn.Module):
    """One-layer LSTM over a single time step followed by a linear head."""

    def __init__(self, feature_count: int, hidden_size: int) -> None:
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=feature_count,
            hidden_size=hidden_size,
            num_layers=DEFAULT_LAYERS,
            batch_first=True,
        )
        self.head = nn.Linear(hidden_size, 1)

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        output, _ = self.lstm(inputs)
        return self.head(output[:, -1, :]).squeeze(-1)


@dataclass(frozen=True)
class LstmForecaster:
    """A fitted LSTM: weights, train-only scalers and the training bookkeeping."""

    state_dict: dict[str, torch.Tensor]
    feature_count: int
    hidden_size: int
    standardizer: Standardizer
    target_mean: float
    target_scale: float
    best_epoch: int
    epochs_run: int
    best_validation_loss: float
    dropped_rows: int
    handles_missing: bool = False
    device: str = DEVICE

    def predict(self, features: object) -> np.ndarray:
        """Predict in original water-level units; rows holding ``NaN`` yield ``NaN``."""
        matrix = as_float_matrix(features, columns=self.feature_count)
        if matrix.shape[0] == 0:
            return np.empty(0, dtype="float64")

        standardised = self.standardizer.transform(matrix)
        module = _build_module(self.feature_count, self.hidden_size)
        module.load_state_dict(self.state_dict)
        module.eval()
        with torch.no_grad():
            inputs = torch.tensor(standardised, dtype=torch.float32).unsqueeze(1)
            output = module(inputs).numpy().astype("float64")
        return output * self.target_scale + self.target_mean


def _build_module(feature_count: int, hidden_size: int) -> _LstmRegressor:
    """Build an empty module with the architecture implied by the frozen hyper-parameters."""
    return _LstmRegressor(feature_count, hidden_size).to(DEVICE)


def fit_lstm(
    train: SegmentSamples,
    validation: SegmentSamples,
    *,
    seed: int,
    hidden_size: int = DEFAULT_HIDDEN_SIZE,
    epochs: int = DEFAULT_EPOCHS,
    patience: int = DEFAULT_PATIENCE,
    batch_size: int = DEFAULT_BATCH_SIZE,
    learning_rate: float = DEFAULT_LEARNING_RATE,
) -> LstmForecaster:
    """Fit one LSTM on CPU with validation-based early stopping.

    Args:
        train: 训练段样本，唯一允许拟合的段。
        validation: 验证段样本，仅用于早停。
        seed: 随机种子；同时驱动参数初始化与批顺序。
        hidden_size: 隐层宽度，默认 32（DS-6 冻结值）。
        epochs: 最大轮数，默认 300（DS-6 冻结上界）。
        patience: 早停耐心，默认 20。
        batch_size: 批大小，默认 64。
        learning_rate: Adam 学习率，默认 0.01。

    Returns:
        :class:`LstmForecaster`；``dropped_rows`` 为训练段因缺失特征被剔除的行数。

    Raises:
        ValueError: 任一超参非法，或剔除缺失行后训练段一个样本都不剩。
    """
    steps = _validated_positive_int(seed, name="seed", allow_zero=True)
    hidden = _validated_positive_int(hidden_size, name="hidden_size")
    maximum_epochs = _validated_positive_int(epochs, name="epochs")
    patience_epochs = _validated_positive_int(patience, name="patience")
    batch = _validated_positive_int(batch_size, name="batch_size")
    if isinstance(learning_rate, bool) or not isinstance(learning_rate, Real):
        raise ValueError(f"learning_rate must be a positive finite number, got {learning_rate!r}")
    rate = float(learning_rate)
    if not np.isfinite(rate) or rate <= 0.0:
        raise ValueError(f"learning_rate must be a positive finite number, got {learning_rate!r}")

    matrix, target, dropped = train.without_missing_features()
    if matrix.shape[0] == 0:
        raise ValueError(
            f"no complete feature row left in the '{train.segment}' segment for "
            f"horizon {train.horizon}; every row carries at least one missing feature"
        )
    validation_matrix, validation_target, _ = validation.without_missing_features()

    standardizer = Standardizer.fit(matrix)
    target_mean = float(target.mean())
    target_span = float(target.std())
    target_scale = target_span if target_span > 0.0 else 1.0

    was_deterministic = torch.are_deterministic_algorithms_enabled()
    torch.use_deterministic_algorithms(True)
    try:
        torch.manual_seed(steps)
        module = _build_module(matrix.shape[1], hidden)
        optimizer = torch.optim.Adam(module.parameters(), lr=rate)
        loss_function = nn.MSELoss()

        inputs = torch.tensor(standardizer.transform(matrix), dtype=torch.float32).unsqueeze(1)
        labels = torch.tensor((target - target_mean) / target_scale, dtype=torch.float32)
        if validation_matrix.shape[0]:
            watch_inputs = torch.tensor(
                standardizer.transform(validation_matrix), dtype=torch.float32
            ).unsqueeze(1)
            watch_labels = torch.tensor(
                (validation_target - target_mean) / target_scale, dtype=torch.float32
            )
        else:
            watch_inputs = inputs
            watch_labels = labels

        generator = torch.Generator().manual_seed(steps)
        best_state = copy.deepcopy(module.state_dict())
        best_loss = float("inf")
        best_epoch = 0
        epochs_run = 0
        stale = 0

        for epoch in range(1, maximum_epochs + 1):
            module.train()
            order = torch.randperm(int(inputs.shape[0]), generator=generator)
            for start in range(0, int(inputs.shape[0]), batch):
                index = order[start : start + batch]
                optimizer.zero_grad()
                loss = loss_function(module(inputs[index]), labels[index])
                loss.backward()
                optimizer.step()

            epochs_run = epoch
            module.eval()
            with torch.no_grad():
                current = float(loss_function(module(watch_inputs), watch_labels).item())
            if current < best_loss - 1e-12:
                best_loss = current
                best_epoch = epoch
                stale = 0
                best_state = copy.deepcopy(module.state_dict())
            else:
                stale += 1
                if stale >= patience_epochs:
                    break
    finally:
        torch.use_deterministic_algorithms(was_deterministic)

    return LstmForecaster(
        state_dict=best_state,
        feature_count=int(matrix.shape[1]),
        hidden_size=hidden,
        standardizer=standardizer,
        target_mean=target_mean,
        target_scale=target_scale,
        best_epoch=best_epoch,
        epochs_run=epochs_run,
        best_validation_loss=best_loss,
        dropped_rows=int(dropped),
    )


def _validated_positive_int(value: object, *, name: str, allow_zero: bool = False) -> int:
    """Return ``value`` as an ``int`` honouring the ``allow_zero`` lower bound."""
    lower = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{name} must be an integer >= {lower}, got {value!r}")
    number = int(value)
    if number < lower:
        raise ValueError(f"{name} must be an integer >= {lower}, got {value!r}")
    return number
