import pytest
import torch
from helpers.context import TestContext
from helpers.dummy import dummy_infinite_data_loader, init_dummy_model
from helpers.utils import (
    available_gpus,
    get_all_3d_configurations,
    init_distributed,
    is_dict_equal,
)
from torch.nn.parallel import DistributedDataParallel

from nanotron.nn import distributed as dist
from nanotron.nn.dataclass import DistributedProcessGroups, RandomStates
from nanotron.nn.gradient_accumulator import FP32GradientAccumulator
from nanotron.nn.optim.named_optimizer import NamedOptimizer
from nanotron.nn.optim.optimizer_from_gradient_accumulator import (
    OptimizerFromGradientAccumulator,
)
from nanotron.nn.optim.zero import ZeroDistributedOptimizer
from nanotron.nn.parallel.pipeline_parallelism.engine import (
    AllForwardAllBackwardPipelineEngine,
)
from nanotron.nn.parallel.sharded_parameters import SplitConfig, create_sharded_parameter_from_config
from nanotron.nn.parallel.tied_parameters import sync_tied_weights_gradients
from nanotron.nn.random import get_current_random_state, get_synced_random_state
from nanotron.nn.serialize import (
    load_optimizer,
    load_random_states,
    load_weights,
    save_optimizer,
    save_random_states,
    save_weights,
)
from nanotron.nn.serialize.constants import CHECKPOINT_VERSION
from nanotron.nn.serialize.meta import TensorMetadataV2


def test_save_and_load_with_changed_topolgy():
    # TODO @thomasw21: We want to be able to support a change of topology mechanism
    return


@pytest.mark.parametrize(
    "tp,dp,pp",
    [
        pytest.param(*all_3d_configs)
        for gpus in range(1, min(available_gpus(), 8) + 1)
        for all_3d_configs in get_all_3d_configurations(gpus)
    ],
)
def test_save_and_load_model(tp: int, dp: int, pp: int):
    test_context = TestContext()
    # We use DP=2 as we're interested in testing that one
    init_distributed(tp=tp, dp=dp, pp=pp)(_test_save_and_load_model)(test_context=test_context)


def _test_save_and_load_model(dpg: DistributedProcessGroups, test_context: TestContext):
    model = init_dummy_model(dpg=dpg)
    store_folder = test_context.get_auto_remove_tmp_dir()

    # Save
    save_weights(model=model, dpg=dpg, root_folder=store_folder)

    # Load
    new_model = init_dummy_model(dpg=dpg)

    # Check that the newly initialised model isn't the same.
    match, msg = is_dict_equal(new_model.state_dict(), model.state_dict())
    if len(model.state_dict()) == 0:
        # Edge case where there's no parameters/buffers stored in the model.
        pass
    else:
        assert not match, "Newly initialised model should not match."

    load_weights(model=new_model, dpg=dpg, root_folder=store_folder)

    # Assert the weights are exactly the same after loading
    match, msg = is_dict_equal(new_model.state_dict(), model.state_dict())
    assert match, msg


@pytest.mark.parametrize(
    "tp,dp,pp",
    [
        pytest.param(*all_3d_configs)
        for gpus in range(1, min(available_gpus(), 8) + 1)
        for all_3d_configs in get_all_3d_configurations(gpus)
    ],
)
def test_save_and_load_optimizer(tp: int, dp: int, pp: int):
    test_context = TestContext()
    # We use DP=2 as we're interested in testing that one
    init_distributed(tp=tp, dp=dp, pp=pp)(_test_save_and_load_optimizer)(test_context=test_context)


def _test_save_and_load_optimizer(dpg: DistributedProcessGroups, test_context: TestContext):
    store_folder = test_context.get_auto_remove_tmp_dir()
    model = init_dummy_model(dpg=dpg)
    optimizer = NamedOptimizer(
        named_params_or_groups=model.named_parameters(),
        optimizer_builder=lambda params: torch.optim.AdamW(params),
    )

    # Train in order to update the optimizer step a few times
    data_loader = iter(dummy_infinite_data_loader(pp_pg=dpg.pp_pg))
    nb_optim_steps = 3
    pipeline_engine = AllForwardAllBackwardPipelineEngine()
    for _ in range(nb_optim_steps):
        minibatch = next(data_loader)
        _ = pipeline_engine.train_batch_iter(model=model, pg=dpg.pp_pg, batch=[minibatch], grad_accumulator=None)
        # Manually sync tied parameters
        sync_tied_weights_gradients(module=model, dpg=dpg, grad_accumulator=None)
        # Optimizer steps
        optimizer.step()
        optimizer.zero_grad()

    # Save optimizer
    save_optimizer(optimizer=optimizer, dpg=dpg, root_folder=store_folder)
    dist.barrier(dpg.world_pg)

    # Generate a new optimizer
    new_optimizer = NamedOptimizer(
        named_params_or_groups=model.named_parameters(),
        optimizer_builder=lambda params: torch.optim.AdamW(params),
    )

    # Check that the newly initialised optimizer isn't the same.
    match, msg = is_dict_equal(optimizer.state_dict(), new_optimizer.state_dict())
    if len(optimizer.state_dict()["state"]) == 0:
        # Edge case where there's no state stored in the optimizer.
        pass
    else:
        assert not match, "Newly initialised optimizer should not match."

    load_optimizer(optimizer=new_optimizer, dpg=dpg, root_folder=store_folder)

    # Assert the optimizer states are exactly the same after loading.
    match, msg = is_dict_equal(optimizer.state_dict(), new_optimizer.state_dict())
    assert match, msg


@pytest.mark.parametrize(
    "tp,dp,pp",
    [
        pytest.param(*all_3d_configs)
        for gpus in range(1, min(available_gpus(), 8) + 1)
        for all_3d_configs in get_all_3d_configurations(gpus)
    ],
)
def test_save_zero_optimizer_and_load_optimizer(tp: int, dp: int, pp: int):
    test_context = TestContext()
    # We use DP=2 as we're interested in testing that one
    init_distributed(tp=tp, dp=dp, pp=pp)(_test_save_zero_optimizer_and_load_optimizer)(test_context=test_context)


def _test_save_zero_optimizer_and_load_optimizer(dpg: DistributedProcessGroups, test_context: TestContext):
    store_folder = test_context.get_auto_remove_tmp_dir()
    model = init_dummy_model(dpg=dpg)
    optimizer = ZeroDistributedOptimizer(
        named_params_or_groups=model.named_parameters(),
        optimizer_builder=lambda named_param_groups: NamedOptimizer(
            named_params_or_groups=named_param_groups,
            optimizer_builder=lambda param_groups: torch.optim.AdamW(param_groups),
        ),
        dp_pg=dpg.dp_pg,
    )

    # Train in order to update the optimizer step a few times
    data_loader = iter(dummy_infinite_data_loader(pp_pg=dpg.pp_pg))
    nb_optim_steps = 3
    pipeline_engine = AllForwardAllBackwardPipelineEngine()
    for _ in range(nb_optim_steps):
        minibatch = next(data_loader)
        _ = pipeline_engine.train_batch_iter(model=model, pg=dpg.pp_pg, batch=[minibatch], grad_accumulator=None)
        # Manually sync tied parameters
        sync_tied_weights_gradients(module=model, dpg=dpg, grad_accumulator=None)
        # Optimizer steps
        optimizer.step()
        optimizer.zero_grad()

    # Save optimizer
    save_optimizer(optimizer=optimizer, dpg=dpg, root_folder=store_folder)
    dist.barrier(dpg.world_pg)

    # Generate a new optimizer
    new_optimizer = ZeroDistributedOptimizer(
        named_params_or_groups=model.named_parameters(),
        optimizer_builder=lambda named_param_groups: NamedOptimizer(
            named_params_or_groups=named_param_groups,
            optimizer_builder=lambda param_groups: torch.optim.AdamW(param_groups),
        ),
        dp_pg=dpg.dp_pg,
    )

    # Check that the newly initialised optimizer isn't the same.
    match, msg = is_dict_equal(optimizer.state_dict(), new_optimizer.state_dict())
    if len(optimizer.state_dict()["state"]) == 0:
        # Edge case where there's no state stored in the optimizer.
        pass
    else:
        assert not match, "Newly initialised optimizer should not match."

    load_optimizer(optimizer=new_optimizer, dpg=dpg, root_folder=store_folder)

    # Assert the optimizer states are exactly the same after loading.
    match, msg = is_dict_equal(optimizer.state_dict(), new_optimizer.state_dict())
    assert match, msg


@pytest.mark.skip(reason="Assumption that zero and non zero optimizer have the same serialization format doesn't hold")
@pytest.mark.parametrize(
    "tp,dp,pp",
    [
        pytest.param(*all_3d_configs)
        for gpus in range(1, min(available_gpus(), 8) + 1)
        for all_3d_configs in get_all_3d_configurations(gpus)
    ],
)
def test_save_zero_optimizer_and_load_data_parallel_optimizer(tp: int, dp: int, pp: int):
    test_context = TestContext()
    # We use DP=2 as we're interested in testing that one
    init_distributed(tp=tp, dp=dp, pp=pp)(_test_save_zero_optimizer_and_load_data_parallel_optimizer)(
        test_context=test_context
    )


def _test_save_zero_optimizer_and_load_data_parallel_optimizer(
    dpg: DistributedProcessGroups, test_context: TestContext
):
    store_folder = test_context.get_auto_remove_tmp_dir()
    model = init_dummy_model(dpg=dpg)
    optimizer = ZeroDistributedOptimizer(
        named_params_or_groups=model.named_parameters(),
        optimizer_builder=lambda named_param_groups: NamedOptimizer(
            named_params_or_groups=named_param_groups,
            optimizer_builder=lambda param_groups: torch.optim.AdamW(param_groups),
        ),
        dp_pg=dpg.dp_pg,
    )

    # Train in order to update the optimizer step a few times
    data_loader = iter(dummy_infinite_data_loader(pp_pg=dpg.pp_pg))
    nb_optim_steps = 3
    pipeline_engine = AllForwardAllBackwardPipelineEngine()
    for _ in range(nb_optim_steps):
        minibatch = next(data_loader)
        _ = pipeline_engine.train_batch_iter(model=model, pg=dpg.pp_pg, batch=[minibatch], grad_accumulator=None)
        # Manually sync tied parameters
        sync_tied_weights_gradients(module=model, dpg=dpg, grad_accumulator=None)
        # Optimizer steps
        optimizer.step()
        optimizer.zero_grad()

    # Save optimizer
    save_optimizer(optimizer=optimizer, dpg=dpg, root_folder=store_folder)
    dist.barrier(dpg.world_pg)

    # Generate a new optimizer
    new_optimizer = NamedOptimizer(
        named_params_or_groups=model.named_parameters(),
        optimizer_builder=lambda params: torch.optim.AdamW(params),
    )

    # Check that the newly initialised optimizer isn't the same.
    match, msg = is_dict_equal(optimizer.state_dict(), new_optimizer.state_dict())
    if len(optimizer.state_dict()["state"]) == 0:
        # Edge case where there's no state stored in the optimizer.
        pass
    else:
        assert not match, "Newly initialised optimizer should not match."

    load_optimizer(optimizer=new_optimizer, dpg=dpg, root_folder=store_folder)

    # TODO @thomasw21: Compare zero optimizer with non zero


@pytest.mark.skip(reason="Assumption that zero and non zero optimizer have the same serialization format doesn't hold")
@pytest.mark.parametrize(
    "tp,dp,pp",
    [
        pytest.param(*all_3d_configs)
        for gpus in range(1, min(available_gpus(), 8) + 1)
        for all_3d_configs in get_all_3d_configurations(gpus)
    ],
)
def test_save_data_parallel_optimizer_and_load_zero_optimizer(tp: int, dp: int, pp: int):
    test_context = TestContext()
    # We use DP=2 as we're interested in testing that one
    init_distributed(tp=tp, dp=dp, pp=pp)(_test_save_data_parallel_optimizer_and_load_zero_optimizer)(
        test_context=test_context
    )


def _test_save_data_parallel_optimizer_and_load_zero_optimizer(
    dpg: DistributedProcessGroups, test_context: TestContext
):
    store_folder = test_context.get_auto_remove_tmp_dir()
    model = init_dummy_model(dpg=dpg)
    optimizer = NamedOptimizer(
        named_params_or_groups=model.named_parameters(),
        optimizer_builder=lambda params: torch.optim.AdamW(params),
    )

    # Train in order to update the optimizer step a few times
    data_loader = iter(dummy_infinite_data_loader(pp_pg=dpg.pp_pg))
    nb_optim_steps = 3
    pipeline_engine = AllForwardAllBackwardPipelineEngine()
    for _ in range(nb_optim_steps):
        minibatch = next(data_loader)
        _ = pipeline_engine.train_batch_iter(model=model, pg=dpg.pp_pg, batch=[minibatch], grad_accumulator=None)
        optimizer.step()
        optimizer.zero_grad()

    # Save optimizer
    save_optimizer(optimizer=optimizer, dpg=dpg, root_folder=store_folder)
    dist.barrier(dpg.world_pg)

    # Generate a new optimizer
    new_optimizer = ZeroDistributedOptimizer(
        named_params_or_groups=model.named_parameters(),
        optimizer_builder=lambda named_param_groups: NamedOptimizer(
            named_params_or_groups=named_param_groups,
            optimizer_builder=lambda param_groups: torch.optim.AdamW(param_groups),
        ),
        dp_pg=dpg.dp_pg,
    )

    # Check that the newly initialised optimizer isn't the same.
    match, msg = is_dict_equal(optimizer.state_dict(), new_optimizer.state_dict())
    if len(optimizer.state_dict()["state"]) == 0:
        # Edge case where there's no state stored in the optimizer.
        pass
    else:
        assert not match, "Newly initialised optimizer should not match."

    load_optimizer(optimizer=new_optimizer, dpg=dpg, root_folder=store_folder)

    # TODO @thomasw21: Compare zero optimizer with non zero


@pytest.mark.parametrize(
    "tp,dp,pp",
    [
        pytest.param(*all_3d_configs)
        for gpus in range(1, min(available_gpus(), 8) + 1)
        for all_3d_configs in get_all_3d_configurations(gpus)
    ],
)
def test_save_optimizer_with_additional_state_dict_keys(tp: int, dp: int, pp: int):
    test_context = TestContext()
    # We use DP=2 as we're interested in testing that one
    init_distributed(tp=tp, dp=dp, pp=pp)(_test_save_optimizer_with_additional_state_dict_keys)(
        test_context=test_context
    )


def _test_save_optimizer_with_additional_state_dict_keys(dpg: DistributedProcessGroups, test_context: TestContext):
    dtype = torch.float16
    store_folder = test_context.get_auto_remove_tmp_dir()
    model = init_dummy_model(dpg=dpg, dtype=dtype)

    if isinstance(model, DistributedDataParallel):
        # Remove the annoying "module." prefix
        normalized_model = model.module
    else:
        normalized_model = model

    named_parameters = list(normalized_model.named_parameters())

    optimizer = OptimizerFromGradientAccumulator(
        gradient_accumulator_builder=lambda named_params: FP32GradientAccumulator(named_parameters=named_params),
        named_params_or_groups=named_parameters,
        optimizer_builder=lambda named_param_groups: NamedOptimizer(
            named_params_or_groups=named_param_groups,
            optimizer_builder=lambda param_groups: torch.optim.AdamW(param_groups),
        ),
    )
    grad_accumulator = optimizer.gradient_accumulator

    assert len(optimizer.state_dict_additional_keys()) > 0

    # Train in order to update the optimizer step a few times
    data_loader = iter(dummy_infinite_data_loader(pp_pg=dpg.pp_pg, dtype=dtype))
    nb_optim_steps = 3
    pipeline_engine = AllForwardAllBackwardPipelineEngine()
    for _ in range(nb_optim_steps):
        minibatch = next(data_loader)
        _ = pipeline_engine.train_batch_iter(
            model=model, pg=dpg.pp_pg, batch=[minibatch], grad_accumulator=grad_accumulator
        )
        # Manually sync tied parameters
        sync_tied_weights_gradients(module=normalized_model, dpg=dpg, grad_accumulator=grad_accumulator)
        # Optimizer steps
        optimizer.step()
        optimizer.zero_grad()

    # Save optimizer
    save_optimizer(optimizer=optimizer, dpg=dpg, root_folder=store_folder)
    dist.barrier(dpg.world_pg)

    # Generate a new optimizer
    new_optimizer = OptimizerFromGradientAccumulator(
        gradient_accumulator_builder=lambda named_params: FP32GradientAccumulator(named_parameters=named_params),
        named_params_or_groups=named_parameters,
        optimizer_builder=lambda named_param_groups: NamedOptimizer(
            named_params_or_groups=named_param_groups,
            optimizer_builder=lambda param_groups: torch.optim.AdamW(param_groups),
        ),
    )
    new_grad_accumulator = new_optimizer.gradient_accumulator

    # Check that the newly initialised optimizer isn't the same.
    if len(optimizer.state_dict()["state"]) == 0:
        pass
    else:
        match, msg = is_dict_equal(optimizer.state_dict(), new_optimizer.state_dict())
        assert not match, "Newly initialised optimizer should not match."

    load_optimizer(optimizer=new_optimizer, dpg=dpg, root_folder=store_folder)

    # Assert the optimizer states are exactly the same after loading.
    match, msg = is_dict_equal(optimizer.state_dict()["state"], new_optimizer.state_dict()["state"])
    assert match, msg

    # Assert the optimizer state_dict are exactly the same after loading.
    match, msg = is_dict_equal(optimizer.state_dict(), new_optimizer.state_dict())
    assert match, msg

    # Assert the internal optimizer states are exactly the same after loading.
    keys_to_ignore = []
    match, msg = is_dict_equal(
        {
            name: {key: tensor for key, tensor in elt.items() if key not in keys_to_ignore}
            for name, elt in grad_accumulator.parameters.items()
        },
        {
            name: {key: tensor for key, tensor in elt.items() if key not in keys_to_ignore}
            for name, elt in new_grad_accumulator.parameters.items()
        },
    )
    assert match, msg


# TODO @thomasw21: Test with a optimizer that uses `named_param_groups` instead of `param_groups`


@pytest.mark.skipif(available_gpus() < 2, reason="Testing test_save_and_load_random_states requires at least 2 gpus")
def test_save_and_load_random_states():
    test_context = TestContext()
    # We use DP=2 as we're interested in testing
    init_distributed(tp=2, dp=1, pp=1)(_test_save_and_load_random_states)(test_context=test_context)


def _test_save_and_load_random_states(dpg: DistributedProcessGroups, test_context: TestContext):
    pg = next((pg for pg in [dpg.tp_pg, dpg.dp_pg, dpg.pp_pg] if pg.size() == 2))
    random_states = RandomStates(
        {
            "my_synced_random_state": get_synced_random_state(random_state=get_current_random_state(), pg=pg),
            "my_own_random_state": get_current_random_state(),
        }
    )
    store_folder = test_context.get_auto_remove_tmp_dir()

    # Check that random states are unequal between ranks (due to `my_own_random_state`)
    reference_rank = 0
    if dist.get_rank(pg) == reference_rank:
        random_statess = [random_states]
    else:
        random_statess = [None]
    dist.broadcast_object_list(random_statess, src=dist.get_global_rank(group_rank=reference_rank, group=pg), group=pg)
    if dist.get_rank(pg) != reference_rank:
        assert random_states != random_statess[0]

    # save
    save_random_states(random_states=random_states, dpg=dpg, root_folder=store_folder)

    # load
    new_random_states = load_random_states(dpg=dpg, root_folder=store_folder)
    # Each rank has restored it's own random state
    assert random_states == new_random_states


def test_serialize_deserialize_tensormetadata():
    test_context = TestContext()
    init_distributed(tp=2, dp=1, pp=1)(_test_serialize_deserialize_tensormetadata)(test_context=test_context)


def _test_serialize_deserialize_tensormetadata(dpg: DistributedProcessGroups, test_context: TestContext):
    param = torch.nn.Parameter(torch.randn(16, 64))
    split_config = SplitConfig(
        split_dim=0,
        contiguous_chunks=(8, 8),
    )
    param = create_sharded_parameter_from_config(parameter=param, pg=dpg.tp_pg, split_config=split_config)
    sharded_info = param.get_sharded_info()
    metadata = TensorMetadataV2(
        version=CHECKPOINT_VERSION,
        local_global_slices_pairs=sharded_info.local_global_slices_pairs,
        unsharded_shape=sharded_info.unsharded_shape,
    )
    metadata_str_dict = metadata.to_str_dict()
    # Assert metadata_str_dict is Dict[str, str]
    assert isinstance(metadata_str_dict, dict)
    assert all(isinstance(key, str) for key in metadata_str_dict.keys())
    assert all(isinstance(value, str) for value in metadata_str_dict.values())

    metadata_from_str_dict = TensorMetadataV2.from_str_dict(metadata_str_dict)
    assert metadata == metadata_from_str_dict
