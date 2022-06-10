import copy
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
import warnings

import torch
from torch.fx import GraphModule
from torch.fx._symbolic_trace import Tracer
from torch.fx.node import Target, Node, Argument
from torch.nn.intrinsic import _FusedModule
from .fx import fuse  # noqa: F401
from .fx import prepare  # noqa: F401
from .fx.convert import convert
from .backend_config import get_tensorrt_backend_config_dict  # noqa: F401
from .fx.graph_module import ObservedGraphModule
from .fx.custom_config import (
    ConvertCustomConfig,
    FuseCustomConfig,
    PrepareCustomConfig,
)
from .fx.utils import graph_pretty_str  # noqa: F401
from .fx.utils import get_custom_module_class_keys  # noqa: F401
from .qconfig_mapping import QConfigMapping

def _check_is_graph_module(model: torch.nn.Module) -> None:
    if not isinstance(model, GraphModule):
        raise ValueError(
            "input model must be a GraphModule, "
            + "Got type:"
            + str(type(model))
            + " Please make "
            + "sure to follow the tutorials."
        )


def _swap_ff_with_fxff(model: torch.nn.Module) -> None:
    r""" Swap FloatFunctional with FXFloatFunctional
    """
    modules_to_swap = []
    for name, module in model.named_children():
        if isinstance(module, torch.nn.quantized.FloatFunctional):
            modules_to_swap.append(name)
        else:
            _swap_ff_with_fxff(module)

    for name in modules_to_swap:
        del model._modules[name]
        model._modules[name] = torch.nn.quantized.FXFloatFunctional()


def _fuse_fx(
    graph_module: GraphModule,
    is_qat: bool,
    fuse_custom_config: Union[FuseCustomConfig, Dict[str, Any], None] = None,
    backend_config_dict: Optional[Dict[str, Any]] = None,
) -> GraphModule:
    r""" Internal helper function to fuse modules in preparation for quantization

    Args:
        graph_module: GraphModule object from symbolic tracing (torch.fx.symbolic_trace)
    """
    _check_is_graph_module(graph_module)
    return fuse(
        graph_module, is_qat, fuse_custom_config, backend_config_dict)  # type: ignore[operator]


class Scope(object):
    """ Scope object that records the module path and the module type
    of a module. Scope is used to track the information of the module
    that contains a Node in a Graph of GraphModule. For example::

        class Sub(torch.nn.Module):
            def forward(self, x):
                # This will be a call_method Node in GraphModule,
                # scope for this would be (module_path="sub", module_type=Sub)
                return x.transpose(1, 2)

        class M(torch.nn.Module):
            def __init__(self):
                self.sub = Sub()

            def forward(self, x):
                # This will be a call_method Node as well,
                # scope for this would be (module_path="", None)
                x = x.transpose(1, 2)
                x = self.sub(x)
                return x

    """

    def __init__(self, module_path: str, module_type: Any):
        super().__init__()
        self.module_path = module_path
        self.module_type = module_type


class ScopeContextManager(object):
    """ A context manager to track the Scope of Node during symbolic tracing.
    When entering a forward function of a Module, we'll update the scope information of
    the current module, and when we exit, we'll restore the previous scope information.
    """

    def __init__(
        self, scope: Scope, current_module: torch.nn.Module, current_module_path: str
    ):
        super().__init__()
        self.prev_module_type = scope.module_type
        self.prev_module_path = scope.module_path
        self.scope = scope
        self.scope.module_path = current_module_path
        self.scope.module_type = type(current_module)

    def __enter__(self):
        return

    def __exit__(self, *args):
        self.scope.module_path = self.prev_module_path
        self.scope.module_type = self.prev_module_type
        return


class QuantizationTracer(Tracer):
    def __init__(
        self, skipped_module_names: List[str], skipped_module_classes: List[Callable]
    ):
        super().__init__()
        self.skipped_module_names = skipped_module_names
        self.skipped_module_classes = skipped_module_classes
        # NB: initialized the module_type of top level module to None
        # we are assuming people won't configure the model with the type of top level
        # module here, since people can use "" for global config
        # We can change this if there is a use case that configures
        # qconfig using top level module type
        self.scope = Scope("", None)
        self.node_name_to_scope: Dict[str, Tuple[str, type]] = {}
        self.record_stack_traces = True

    def is_leaf_module(self, m: torch.nn.Module, module_qualified_name: str) -> bool:
        return (
            (
                m.__module__.startswith("torch.nn")
                and not isinstance(m, torch.nn.Sequential)
            )
            or module_qualified_name in self.skipped_module_names
            or type(m) in self.skipped_module_classes
            or isinstance(m, _FusedModule)
        )

    def call_module(
        self,
        m: torch.nn.Module,
        forward: Callable[..., Any],
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
    ) -> Any:
        module_qualified_name = self.path_of_module(m)
        # Creating scope with information of current module
        # scope will be restored automatically upon exit
        with ScopeContextManager(self.scope, m, module_qualified_name):
            return super().call_module(m, forward, args, kwargs)

    def create_node(
        self,
        kind: str,
        target: Target,
        args: Tuple[Argument, ...],
        kwargs: Dict[str, Argument],
        name: Optional[str] = None,
        type_expr: Optional[Any] = None,
    ) -> Node:
        node = super().create_node(kind, target, args, kwargs, name, type_expr)
        self.node_name_to_scope[node.name] = (
            self.scope.module_path,
            self.scope.module_type,
        )
        return node


def _prepare_fx(
    model: torch.nn.Module,
    qconfig_mapping: Union[QConfigMapping, Dict[str, Any]],
    is_qat: bool,
    example_inputs: Tuple[Any, ...],
    prepare_custom_config: Union[PrepareCustomConfig, Dict[str, Any], None] = None,
    equalization_config: Optional[Union[QConfigMapping, Dict[str, Any]]] = None,
    backend_config_dict: Optional[Dict[str, Any]] = None,
    is_standalone_module: bool = False,
) -> ObservedGraphModule:
    r""" Internal helper function for prepare_fx
    Args:
      `model`, `qconfig_mapping`, `prepare_custom_config`, `equalization_config`:
      see docs for :func:`~torch.ao.quantization.prepare_fx`
      `is_standalone_module`: a boolean flag indicates whether we are
      quantizing a standalone module or not, a standalone module
      is a submodule of the parent module that is not inlined in the
forward graph of the parent module,
      the way we quantize standalone module is described in:
      :func:`~torch.ao.quantization._prepare_standalone_module_fx`
    """
    if prepare_custom_config is None:
        prepare_custom_config = PrepareCustomConfig()
    if equalization_config is None:
        equalization_config = QConfigMapping()

    if isinstance(prepare_custom_config, Dict):
        warnings.warn(
            "Passing a prepare_custom_config_dict to prepare is deprecated and will not be supported "
            "in a future version. Please pass in a PrepareCustomConfig instead.")
        prepare_custom_config = PrepareCustomConfig.from_dict(prepare_custom_config)

    skipped_module_names = copy.copy(prepare_custom_config.non_traceable_module_names)
    skipped_module_classes = copy.copy(prepare_custom_config.non_traceable_module_classes)

    # swap FloatFunctional with FXFloatFunctional
    _swap_ff_with_fxff(model)

    # symbolically trace the model
    if not is_standalone_module:
        # standalone module and custom module config are applied in top level module
        skipped_module_names += list(prepare_custom_config.standalone_module_names.keys())
        skipped_module_classes += list(prepare_custom_config.standalone_module_classes.keys())
        skipped_module_classes += get_custom_module_class_keys(prepare_custom_config.float_to_observed_mapping)

    preserved_attributes = prepare_custom_config.preserved_attributes
    tracer = QuantizationTracer(skipped_module_names, skipped_module_classes)  # type: ignore[arg-type]
    graph_module = GraphModule(model, tracer.trace(model))
    for attr_name in preserved_attributes:
        setattr(graph_module, attr_name, getattr(model, attr_name))
    fuse_custom_config = FuseCustomConfig().set_preserved_attributes(prepare_custom_config.preserved_attributes)
    graph_module = _fuse_fx(
        graph_module,
        is_qat,
        fuse_custom_config,
        backend_config_dict)
    prepared = prepare(
        graph_module,
        qconfig_mapping,
        is_qat,
        tracer.node_name_to_scope,
        example_inputs=example_inputs,
        prepare_custom_config=prepare_custom_config,
        equalization_config=equalization_config,
        backend_config_dict=backend_config_dict,
        is_standalone_module=is_standalone_module,
    )  # type: ignore[operator]

    for attr_name in preserved_attributes:
        setattr(prepared, attr_name, getattr(model, attr_name))
    return prepared


def _prepare_standalone_module_fx(
    model: torch.nn.Module,
    qconfig_mapping: Union[QConfigMapping, Dict[str, Any]],
    is_qat: bool,
    example_inputs: Tuple[Any, ...],
    prepare_custom_config: Union[PrepareCustomConfig, Dict[str, Any], None] = None,
    backend_config_dict: Optional[Dict[str, Any]] = None,
) -> GraphModule:
    r""" [Internal use only] Prepare a standalone module, so that it can be used when quantizing the
    parent module.
    standalone_module means it a submodule that is not inlined in parent module,
    and will be quantized separately as one unit.

    How the standalone module is observed is specified by `input_quantized_idxs` and
    `output_quantized_idxs` in the prepare_custom_config for the standalone module

    Returns:

        * model(GraphModule): prepared standalone module. It has these attributes:

            * `_standalone_module_input_quantized_idxs(List[Int])`: a list of
              indexes for the graph input that is expected to be quantized,
              same as input_quantized_idxs configuration provided
              for the standalone module
            * `_standalone_module_output_quantized_idxs(List[Int])`: a list of
              indexs for the graph output that is quantized
              same as input_quantized_idxs configuration provided
              for the standalone module

    """
    return _prepare_fx(
        model,
        qconfig_mapping,
        is_qat,
        example_inputs,
        prepare_custom_config,
        backend_config_dict=backend_config_dict,
        is_standalone_module=True,
    )


def fuse_fx(
    model: torch.nn.Module,
    fuse_custom_config: Union[FuseCustomConfig, Dict[str, Any], None] = None,
    backend_config_dict: Optional[Dict[str, Any]] = None,
) -> GraphModule:
    # TODO: update this comment
    r""" Fuse modules like conv+bn, conv+bn+relu etc, model must be in eval mode.
    Fusion rules are defined in torch.quantization.fx.fusion_pattern.py

    Args:

        * `model`: a torch.nn.Module model
        * `fuse_custom_config_dict`: Dictionary for custom configurations for fuse_fx, e.g.::

            fuse_custom_config_dict = {
              # Attributes that are not used in forward function will
              # be removed when constructing GraphModule, this is a list of attributes
              # to preserve as an attribute of the GraphModule even when they are
              # not used in the code, these attributes will also persist through deepcopy
              "preserved_attributes": ["preserved_attr"],
            }

    Example::

        from torch.ao.quantization import fuse_fx
        m = Model().eval()
        m = fuse_fx(m)

    """
    if fuse_custom_config is None:
        fuse_custom_config = FuseCustomConfig()

    if isinstance(fuse_custom_config, Dict):
        warnings.warn(
            "Passing a fuse_custom_config_dict to fuse is deprecated and will not be supported "
            "in a future version. Please pass in a FuseCustomConfig instead.")
        fuse_custom_config = FuseCustomConfig.from_dict(fuse_custom_config)

    torch._C._log_api_usage_once("quantization_api.quantize_fx.fuse_fx")
    graph_module = torch.fx.symbolic_trace(model)
    preserved_attributes: Set[str] = set()
    if fuse_custom_config:
        preserved_attributes = set(fuse_custom_config.preserved_attributes)
    for attr_name in preserved_attributes:
        setattr(graph_module, attr_name, getattr(model, attr_name))
    return _fuse_fx(graph_module, False, fuse_custom_config, backend_config_dict)


def prepare_fx(
    model: torch.nn.Module,
    qconfig_mapping: Union[QConfigMapping, Dict[str, Any]],
    example_inputs: Tuple[Any, ...],
    prepare_custom_config: Union[PrepareCustomConfig, Dict[str, Any], None] = None,
    equalization_config: Optional[Union[QConfigMapping, Dict[str, Any]]] = None,
    backend_config_dict: Optional[Dict[str, Any]] = None,
) -> ObservedGraphModule:
    # TODO: update this comment
    r""" Prepare a model for post training static quantization

    Args:
      * `model` (required): torch.nn.Module model, must be in eval mode

      * `qconfig_mapping` (required): mapping from model ops to qconfigs::

          from torch.quantization import QConfigMapping

          qconfig_mapping = QConfigMapping() \
              .set_global(global_qconfig) \
              .set_object_type(torch.nn.Linear, qconfig1) \
              .set_object_type(torch.nn.functional.linear, qconfig1) \
              .set_module_name_regex("foo.*bar.*conv[0-9]+", qconfig1) \
              .set_module_name_regex("foo.*bar.*", qconfig2) \
              .set_module_name_regex("foo.*", qconfig3) \
              .set_module_name("module1", qconfig1) \
              .set_module_name("module2", qconfig2) \
              .set_module_name_object_type_order("module3", torch.nn.functional.linear, 0, qconfig3)

      * `example_inputs`: (required) Example inputs for forward function of the model

      * `prepare_custom_config`: customization configuration for quantization tool::

          prepare_custom_config_dict = {
            # optional: specify the path for standalone modules
            # These modules are symbolically traced and quantized as one unit
            "standalone_module_name": [
               # module_name, qconfig_dict, prepare_custom_config_dict
               ("submodule.standalone",
                None,  # qconfig_dict for the prepare function called in the submodule,
                       # None means use qconfig from parent qconfig_dict
                {"input_quantized_idxs": [], "output_quantized_idxs": []}),  # prepare_custom_config_dict
                {}  # backend_config_dict, TODO: point to README doc when it's ready
            ],

            "standalone_module_class": [
                # module_class, qconfig_dict, prepare_custom_config_dict
                (StandaloneModule,
                 None,  # qconfig_dict for the prepare function called in the submodule,
                        # None means use qconfig from parent qconfig_dict
                {"input_quantized_idxs": [0], "output_quantized_idxs": [0]},  # prepare_custom_config_dict
                {})  # backend_config_dict, TODO: point to README doc when it's ready
            ],

            # user will manually define the corresponding observed
            # module class which has a from_float class method that converts
            # float custom module to observed custom module
            # (only needed for static quantization)
            "float_to_observed_custom_module_class": {
               "static": {
                   CustomModule: ObservedCustomModule
               }
            },

            # the qualified names for the submodule that are not symbolically traceable
            "non_traceable_module_name": [
               "non_traceable_module"
            ],

            # the module classes that are not symbolically traceable
            # we'll also put dynamic/weight_only custom module here
            "non_traceable_module_class": [
               NonTraceableModule
            ],

            # By default, inputs and outputs of the graph are assumed to be in
            # fp32. Providing `input_quantized_idxs` will set the inputs with the
            # corresponding indices to be quantized. Providing
            # `output_quantized_idxs` will set the outputs with the corresponding
            # indices to be quantized.
            "input_quantized_idxs": [0],
            "output_quantized_idxs": [0],

            # Attributes that are not used in forward function will
            # be removed when constructing GraphModule, this is a list of attributes
            # to preserve as an attribute of the GraphModule even when they are
            # not used in the code, these attributes will also persist through deepcopy
            "preserved_attributes": ["preserved_attr"],
          }

      * `equalization_config`: config for specifying how to perform equalization on the model

      * `backend_config_dict`: a dictionary that specifies how operators are quantized
         in a backend, this includes how the operaetors are observed,
         supported fusion patterns, how quantize/dequantize ops are
         inserted, supported dtypes etc. The structure of the dictionary is still WIP
         and will change in the future, please don't use right now.

    Return:
      A GraphModule with observer (configured by qconfig_mapping), ready for calibration

    Example::

        import torch
        from torch.ao.quantization import get_default_qconfig
        from torch.ao.quantization import prepare_fx

        float_model.eval()
        qconfig = get_default_qconfig('fbgemm')
        def calibrate(model, data_loader):
            model.eval()
            with torch.no_grad():
                for image, target in data_loader:
                    model(image)

        qconfig_mapping = QConfigMapping().set_global(qconfig)
        example_inputs = (torch.randn(1, 3, 224, 224),)
        prepared_model = prepare_fx(float_model, qconfig_mapping, example_inputs)
        # Run calibration
        calibrate(prepared_model, sample_inference_data)
    """
    torch._C._log_api_usage_once("quantization_api.quantize_fx.prepare_fx")
    return _prepare_fx(
        model,
        qconfig_mapping,
        False,  # is_qat
        example_inputs,
        prepare_custom_config,
        equalization_config,
        backend_config_dict,
    )


def prepare_qat_fx(
    model: torch.nn.Module,
    qconfig_mapping: Union[QConfigMapping, Dict[str, Any]],
    example_inputs: Tuple[Any, ...],
    prepare_custom_config: Union[PrepareCustomConfig, Dict[str, Any], None] = None,
    backend_config_dict: Optional[Dict[str, Any]] = None,
) -> ObservedGraphModule:
    r""" Prepare a model for quantization aware training

    Args:
      * `model`: torch.nn.Module model, must be in train mode
      * `qconfig_mapping`: see :func:`~torch.ao.quantization.prepare_fx`
      * `example_inputs`: see :func:`~torch.ao.quantization.prepare_fx`
      * `prepare_custom_config`: see :func:`~torch.ao.quantization.prepare_fx`
      * `backend_config_dict`: see :func:`~torch.ao.quantization.prepare_fx`

    Return:
      A GraphModule with fake quant modules (configured by qconfig_mapping), ready for
      quantization aware training

    Example::

        import torch
        from torch.ao.quantization import get_default_qat_qconfig
        from torch.ao.quantization import prepare_fx

        qconfig = get_default_qat_qconfig('fbgemm')
        def train_loop(model, train_data):
            model.train()
            for image, target in data_loader:
                ...

        float_model.train()
        qconfig_mapping = QConfigMapping().set_global(qconfig)
        prepared_model = prepare_fx(float_model, qconfig_mapping)
        # Run calibration
        train_loop(prepared_model, train_loop)

    """
    torch._C._log_api_usage_once("quantization_api.quantize_fx.prepare_qat_fx")
    return _prepare_fx(
        model,
        qconfig_mapping,
        True,  # is_qat
        example_inputs,
        prepare_custom_config,
        backend_config_dict=backend_config_dict,
    )


def _convert_fx(
    graph_module: GraphModule,
    is_reference: bool,
    convert_custom_config: Union[ConvertCustomConfig, Dict[str, Any], None] = None,
    is_standalone_module: bool = False,
    _remove_qconfig: bool = True,
    qconfig_mapping: Union[QConfigMapping, Dict[str, Any], None] = None,
    backend_config_dict: Dict[str, Any] = None,
) -> torch.nn.Module:
    """ `is_standalone_module`: see docs in :func:`~torch.ao.quantization.prepare_standalone_module_fx`
    """
    if convert_custom_config is None:
        convert_custom_config = ConvertCustomConfig()

    if isinstance(convert_custom_config, Dict):
        warnings.warn(
            "Passing a convert_custom_config_dict to convert is deprecated and will not be supported "
            "in a future version. Please pass in a ConvertCustomConfig instead.")
        convert_custom_config = ConvertCustomConfig.from_dict(convert_custom_config)

    _check_is_graph_module(graph_module)

    quantized = convert(
        graph_module,
        is_reference,
        convert_custom_config,
        is_standalone_module,
        _remove_qconfig_flag=_remove_qconfig,
        qconfig_mapping=qconfig_mapping,
        backend_config_dict=backend_config_dict,
    )

    preserved_attributes = convert_custom_config.preserved_attributes
    for attr_name in preserved_attributes:
        setattr(quantized, attr_name, getattr(graph_module, attr_name))
    return quantized


def convert_fx(
    graph_module: GraphModule,
    is_reference: bool = False,
    convert_custom_config: Union[ConvertCustomConfig, Dict[str, Any], None] = None,
    _remove_qconfig: bool = True,
    qconfig_mapping: Union[QConfigMapping, Dict[str, Any]] = None,
    backend_config_dict: Dict[str, Any] = None,
) -> torch.nn.Module:
    # TODO(andrew): Update this comment
    r""" Convert a calibrated or trained model to a quantized model

    Args:
        * `graph_module`: A prepared and calibrated/trained model (GraphModule)
        * `is_reference`: flag for whether to produce a reference quantized model,
          which will be a common interface between pytorch quantization with
          other backends like accelerators
        * `convert_custom_config`: dictionary for custom configurations for convert function::

            convert_custom_config_dict = {
              # user will manually define the corresponding quantized
              # module class which has a from_observed class method that converts
              # observed custom module to quantized custom module
              "observed_to_quantized_custom_module_class": {
                 "static": {
                     ObservedCustomModule: QuantizedCustomModule
                 },
                 "dynamic": {
                     ObservedCustomModule: QuantizedCustomModule
                 },
                 "weight_only": {
                     ObservedCustomModule: QuantizedCustomModule
                 }
              },

              # Attributes that are not used in forward function will
              # be removed when constructing GraphModule, this is a list of attributes
              # to preserve as an attribute of the GraphModule even when they are
              # not used in the code
              "preserved_attributes": ["preserved_attr"],
            }

        * `_remove_qconfig`: Option to remove the qconfig attributes in the model after convert.

        * `qconfig_mapping`: config for specifying how to convert a model for quantization.

           The keys must include the ones in the qconfig_mapping passed to `prepare_fx` or `prepare_qat_fx`,
           with the same values or `None`. Additional keys can be specified with values set to `None`.

          For each entry whose value is set to None, we skip quantizing that entry in the model::

            qconfig_mapping = QConfigMapping
                .set_global(qconfig_from_prepare)
                .set_object_type(torch.nn.functional.add, None)  # skip quantizing torch.nn.functional.add
                .set_object_type(torch.nn.functional.linear, qconfig_from_prepare)
                .set_module_name("foo.bar", None)  # skip quantizing module "foo.bar"

         * `backend_config_dict`: A configuration for the backend which describes how
            operators should be quantized in the backend, this includes quantization
            mode support (static/dynamic/weight_only), dtype support (quint8/qint8 etc.),
            observer placement for each operators and fused operators. Detailed
            documentation can be found in torch/ao/quantization/backend_config/README.md

    Return:
        A quantized model (GraphModule)

    Example::

        # prepared_model: the model after prepare_fx/prepare_qat_fx and calibration/training
        quantized_model = convert_fx(prepared_model)

    """
    torch._C._log_api_usage_once("quantization_api.quantize_fx.convert_fx")
    return _convert_fx(
        graph_module,
        is_reference,
        convert_custom_config,
        _remove_qconfig=_remove_qconfig,
        qconfig_mapping=qconfig_mapping,
        backend_config_dict=backend_config_dict,
    )


def _convert_standalone_module_fx(
    graph_module: GraphModule,
    is_reference: bool = False,
    convert_custom_config: Union[ConvertCustomConfig, Dict[str, Any], None] = None,
) -> torch.nn.Module:
    r""" [Internal use only] Convert a model produced by :func:`~torch.ao.quantization.prepare_standalone_module_fx`
    and convert it to a quantized model

    Returns a quantized standalone module, whether input/output is quantized is
    specified by prepare_custom_config, with
    input_quantized_idxs, output_quantized_idxs, please
    see docs for prepare_fx for details
    """
    return _convert_fx(
        graph_module,
        is_reference,
        convert_custom_config,
        is_standalone_module=True,
    )
