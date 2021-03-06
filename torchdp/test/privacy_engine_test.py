#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import unittest

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torchdp import PrivacyEngine, utils
from torchdp.dp_model_inspector import IncompatibleModuleException
from torchvision import models, transforms
from torchvision.datasets import FakeData


def is_grad_sample_consistent(tensor: torch.Tensor, loss_type: str = "mean"):
    if tensor.grad is None:
        return True

    if tensor.grad_sample is None:
        raise ValueError(
            f"The input tensor {tensor} has grad computed, but missing grad_sample."
            f"Please attach PrivacyEngine"
        )

    if loss_type not in ("sum", "mean"):
        raise ValueError(f"loss_type = {loss_type}. Only 'sum' and 'mean' supported")

    grad_sample_aggregated = torch.einsum("i...->...", tensor.grad_sample)
    if loss_type == "mean":
        b_sz = tensor.grad_sample.shape[0]
        grad_sample_aggregated /= b_sz

    return torch.allclose(tensor.grad, grad_sample_aggregated, atol=10e-5, rtol=10e-2)


class SampleConvNet(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv2d(1, 16, 8, 3)
        self.gnorm1 = nn.GroupNorm(4, 16)
        self.conv2 = nn.Conv1d(16, 32, 3, 1)
        self.lnorm1 = nn.LayerNorm((32, 23))
        self.conv3 = nn.Conv1d(32, 32, 3, 1)
        self.instnorm1 = nn.InstanceNorm1d(32, affine=True)
        self.convf = nn.Conv1d(32, 32, 1, 1)
        for p in self.convf.parameters():
            p.requires_grad = False
        self.fc1 = nn.Linear(21, 17)
        self.lnorm2 = nn.LayerNorm(17)
        self.fc2 = nn.Linear(32 * 17, 10)

        for layer in (self.gnorm1, self.lnorm1, self.lnorm2, self.instnorm1):
            nn.init.uniform_(layer.weight)
            nn.init.uniform_(layer.bias)

    def forward(self, x):
        # x of shape [B, 1, 28, 28]
        x = self.conv1(x)  # -> [B, 16, 10, 10]
        x = self.gnorm1(x)  # -> [B, 16, 10, 10]
        x = F.relu(x)  # -> [B, 16, 10, 10]
        x = F.max_pool2d(x, 2, 2)  # -> [B, 16, 5, 5]
        x = x.view(x.shape[0], x.shape[1], x.shape[2] * x.shape[3])  # -> [B, 16, 25]
        x = self.conv2(x)  # -> [B, 32, 23]
        x = self.lnorm1(x)  # -> [B, 32, 23]
        x = F.relu(x)  # -> [B, 32, 23]
        x = self.conv3(x)  # -> [B, 32, 21]
        x = self.instnorm1(x)  # -> [B, 32, 21]
        x = self.convf(x)  # -> [B, 32, 21]
        x = self.fc1(x)  # -> [B, 32, 17]
        x = self.lnorm2(x)  # -> [B, 32, 17]
        x = x.view(-1, x.shape[-2] * x.shape[-1])  # -> [B, 32 * 17]
        x = self.fc2(x)  # -> [B, 10]
        return x

    def name(self):
        return "SampleConvNet"


class PrivacyEngine_test(unittest.TestCase):
    def setUp(self):
        self.DATA_SIZE = 64
        self.BATCH_SIZE = 64
        self.LR = 0.5
        self.ALPHAS = [1 + x / 10.0 for x in range(1, 100, 10)]
        self.criterion = nn.CrossEntropyLoss()

        self.setUp_data()
        self.original_model, self.original_optimizer = self.setUp_init_model()
        self.private_model, self.private_optimizer = self.setUp_init_model(
            private=True,
            state_dict=self.original_model.state_dict(),
            noise_multiplier=1.3,
            max_grad_norm=1.0,
        )

        self.original_grads_norms = self.setUp_model_step(
            self.original_model, self.original_optimizer
        )
        self.private_grads_norms = self.setUp_model_step(
            self.private_model, self.private_optimizer
        )
        self.privacy_default_params = {'noise_multiplier' : 1.0, 'max_grad_norm' : 1}

    def setUp_data(self):
        self.ds = FakeData(
            size=self.DATA_SIZE,
            image_size=(1, 35, 35),
            num_classes=10,
            transform=transforms.Compose(
                [transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))]
            ),
        )
        self.dl = DataLoader(self.ds, batch_size=self.BATCH_SIZE)

    def setUp_init_model(self, private=False, state_dict=None, model=None, **privacy_engine_kwargs):
        model = model or SampleConvNet()
        optimizer = torch.optim.SGD(model.parameters(), lr=self.LR, momentum=0)
        if state_dict:
            model.load_state_dict(state_dict)

        if private:
            if len(privacy_engine_kwargs) == 0:
                privacy_engine_kwargs = self.privacy_default_params
            privacy_engine = PrivacyEngine(
                model,
                batch_size=self.BATCH_SIZE,
                sample_size=self.DATA_SIZE,
                alphas=self.ALPHAS,
                **privacy_engine_kwargs,
            )
            privacy_engine.attach(optimizer)

        return model, optimizer

    def setUp_model_step(self, model: nn.Module, optimizer: torch.optim.Optimizer):

        for x, y in self.dl:
            optimizer.zero_grad()
            logits = model(x)
            loss = self.criterion(logits, y)
            loss.backward()
            optimizer.step()
        return torch.stack(
            [p.grad.norm() for p in model.parameters() if p.requires_grad], dim=-1
        )

    def test_throws_double_attach(self):
        model, optimizer = self.setUp_init_model(private=True)
        self.setUp_model_step(model, optimizer)
        with self.assertRaises(ValueError):
            model, optimizer = self.setUp_init_model(private=True, model=model)
            self.setUp_model_step(model, optimizer)

    def test_attach_delete_attach(self):
        model, optimizer = self.setUp_init_model(private=True)
        self.setUp_model_step(model, optimizer)
        del optimizer.privacy_engine
        model, optimizer = self.setUp_init_model(private=True, model=model)
        self.setUp_model_step(model, optimizer)

    def test_attach_detach_attach(self):
        model, optimizer = self.setUp_init_model(private=True)
        self.setUp_model_step(model, optimizer)
        optimizer.privacy_engine.detach()
        model, optimizer = self.setUp_init_model(private=True, model=model)
        self.setUp_model_step(model, optimizer)

    def test_privacy_analysis_alpha_in_alphas(self):
        target_delta = 1e-5
        eps, alpha = self.private_optimizer.privacy_engine.get_privacy_spent(
            target_delta
        )
        self.assertTrue(alpha in self.ALPHAS)

    def test_privacy_analysis_epsilon(self):
        target_delta = 1e-5
        eps, alpha = self.private_optimizer.privacy_engine.get_privacy_spent(
            target_delta
        )
        self.assertTrue(eps > 0)

    def test_gradients_change(self):
        """
        Test that gradients are different after one step of SGD
        """
        for layer_grad, private_layer_grad in zip(
            [p for p in self.original_model.parameters() if p.requires_grad],
            [p for p in self.private_model.parameters() if p.requires_grad],
        ):
            self.assertFalse(torch.allclose(layer_grad, private_layer_grad))

    def test_model_weights_change(self):
        """
        Test that the updated models are different after one step of SGD
        """
        for layer, private_layer in zip(
            [p for p in self.original_model.parameters() if p.requires_grad],
            [p for p in self.private_model.parameters() if p.requires_grad],
        ):
            self.assertFalse(torch.allclose(layer, private_layer))

    def test_grad_consistency(self):
        model, optimizer = self.setUp_init_model(
            private=True,
            state_dict=self.original_model.state_dict(),
            noise_multiplier=0,
            max_grad_norm=999,
        )
        self.setUp_model_step(model, optimizer)

        for layer_name, layer in model.named_modules():
            if utils.get_layer_type(layer) == "SampleConvNet":
                continue

            for param_tensor in layer.parameters():
                self.assertTrue(
                    is_grad_sample_consistent(param_tensor),
                    f"grad_sample doesn't match grad. "
                    f"Layer: {layer_name}, Tensor: {param_tensor.shape}",
                )

    def test_grad_matches_original(self):
        original_model, orignial_optimizer = self.setUp_init_model()
        private_model, private_optimizer = self.setUp_init_model(
            private=True,
            state_dict=original_model.state_dict(),
            noise_multiplier=0,
            max_grad_norm=999,
        )

        for _ in range(3):
            self.setUp_model_step(original_model, orignial_optimizer)
            self.setUp_model_step(private_model, private_optimizer)

        for layer_name, private_layer in private_model.named_children():
            if not utils.requires_grad(private_layer):
                continue

            original_layer = getattr(original_model, layer_name)

            for layer, private_layer in zip(
                [p.grad for p in original_layer.parameters() if p.requires_grad],
                [p.grad for p in private_layer.parameters() if p.requires_grad],
            ):
                self.assertTrue(
                    torch.allclose(layer, private_layer, atol=10e-4, rtol=10e-2),
                    f"Layer: {layer_name}. Private gradients with noise 0 doesn't match original",
                )

    def test_noise_changes_every_time(self):
        """
        Test that adding noise results in ever different model params.
        We disable clipping in this test by setting it to a very high threshold.
        """
        model, optimizer = self.setUp_init_model(
            private=True,
            state_dict=self.original_model.state_dict(),
            noise_multiplier=1.3,
            max_grad_norm=999,
        )
        self.setUp_model_step(model, optimizer)
        first_run_params = (p for p in model.parameters() if p.requires_grad)

        model, optimizer = self.setUp_init_model(
            private=True,
            state_dict=self.original_model.state_dict(),
            noise_multiplier=1.3,
            max_grad_norm=999,
        )
        self.setUp_model_step(model, optimizer)
        second_run_params = (p for p in model.parameters() if p.requires_grad)
        for p0, p1 in zip(first_run_params, second_run_params):
            self.assertFalse(torch.allclose(p0, p1))

    def test_model_validator(self):
        """
        Test that the privacy engine throws on attach
        if there are unsupported modules
        """
        privacy_engine = PrivacyEngine(
            models.resnet18(),
            batch_size=self.BATCH_SIZE,
            sample_size=self.DATA_SIZE,
            alphas=self.ALPHAS,
            noise_multiplier=1.3,
            max_grad_norm=1,
        )
        with self.assertRaises(IncompatibleModuleException):
            privacy_engine.attach(self.private_optimizer)
