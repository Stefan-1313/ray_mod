import uuid
import logging
import inspect
from functools import wraps

from ray import cloudpickle as pickle
from ray._raylet import PythonFunctionDescriptor
from ray import cross_language, Language
from ray._private.client_mode_hook import client_mode_convert_function
from ray._private.client_mode_hook import client_mode_should_convert
from ray.util.placement_group import (
    PlacementGroup,
    check_placement_group_index,
    get_current_placement_group,
)
import ray._private.signature
import ray._private.runtime_env as runtime_support
from ray.util.tracing.tracing_helper import (_tracing_task_invocation,
                                             _inject_tracing_into_function)

# Default parameters for remote functions.
DEFAULT_REMOTE_FUNCTION_CPUS = 1
DEFAULT_REMOTE_FUNCTION_NUM_RETURN_VALS = 1
DEFAULT_REMOTE_FUNCTION_MAX_CALLS = 0
# Normal tasks may be retried on failure this many times.
# TODO(swang): Allow this to be set globally for an application.
DEFAULT_REMOTE_FUNCTION_NUM_TASK_RETRIES = 3
DEFAULT_REMOTE_FUNCTION_RETRY_EXCEPTIONS = False

logger = logging.getLogger(__name__)


class RemoteFunction:
    """A remote function.

    This is a decorated function. It can be used to spawn tasks.

    Attributes:
        _language: The target language.
        _function: The original function.
        _function_descriptor: The function descriptor. This is not defined
            until the remote function is first invoked because that is when the
            function is pickled, and the pickled function is used to compute
            the function descriptor.
        _function_name: The module and function name.
        _num_cpus: The default number of CPUs to use for invocations of this
            remote function.
        _num_gpus: The default number of GPUs to use for invocations of this
            remote function.
        _memory: The heap memory request for this task.
        _object_store_memory: The object store memory request for this task.
        _resources: The default custom resource requirements for invocations of
            this remote function.
        _num_returns: The default number of return values for invocations
            of this remote function.
        _max_calls: The number of times a worker can execute this function
            before exiting.
        _max_retries: The number of times this task may be retried
            on worker failure.
        _retry_exceptions: Whether application-level errors should be retried.
        _runtime_env: The runtime environment for this task.
        _decorator: An optional decorator that should be applied to the remote
            function invocation (as opposed to the function execution) before
            invoking the function. The decorator must return a function that
            takes in two arguments ("args" and "kwargs"). In most cases, it
            should call the function that was passed into the decorator and
            return the resulting ObjectRefs. For an example, see
            "test_decorated_function" in "python/ray/tests/test_basic.py".
        _function_signature: The function signature.
        _last_export_session_and_job: A pair of the last exported session
            and job to help us to know whether this function was exported.
            This is an imperfect mechanism used to determine if we need to
            export the remote function again. It is imperfect in the sense that
            the actor class definition could be exported multiple times by
            different workers.
    """

    def __init__(self, language, function, function_descriptor, num_cpus,
                 num_gpus, memory, object_store_memory, resources,
                 accelerator_type, num_returns, max_calls, max_retries,
                 retry_exceptions, runtime_env):
        if inspect.iscoroutinefunction(function):
            raise ValueError("'async def' should not be used for remote "
                             "tasks. You can wrap the async function with "
                             "`asyncio.get_event_loop.run_until(f())`. "
                             "See more at docs.ray.io/async_api.html")
        self._language = language
        self._function = _inject_tracing_into_function(function)
        self._function_name = (function.__module__ + "." + function.__name__)
        self._function_descriptor = function_descriptor
        self._is_cross_language = language != Language.PYTHON
        self._num_cpus = (DEFAULT_REMOTE_FUNCTION_CPUS
                          if num_cpus is None else num_cpus)
        self._num_gpus = num_gpus
        self._memory = memory
        if object_store_memory is not None:
            raise NotImplementedError(
                "setting object_store_memory is not implemented for tasks")
        self._object_store_memory = None
        self._resources = resources
        self._accelerator_type = accelerator_type
        self._num_returns = (DEFAULT_REMOTE_FUNCTION_NUM_RETURN_VALS
                             if num_returns is None else num_returns)
        self._max_calls = (DEFAULT_REMOTE_FUNCTION_MAX_CALLS
                           if max_calls is None else max_calls)
        self._max_retries = (DEFAULT_REMOTE_FUNCTION_NUM_TASK_RETRIES
                             if max_retries is None else max_retries)
        self._retry_exceptions = (DEFAULT_REMOTE_FUNCTION_RETRY_EXCEPTIONS
                                  if retry_exceptions is None else
                                  retry_exceptions)
        self._runtime_env = runtime_env
        self._decorator = getattr(function, "__ray_invocation_decorator__",
                                  None)
        self._function_signature = ray._private.signature.extract_signature(
            self._function)

        self._last_export_session_and_job = None
        self._uuid = uuid.uuid4()

        # Override task.remote's signature and docstring
        @wraps(function)
        def _remote_proxy(*args, **kwargs):
            return self._remote(args=args, kwargs=kwargs)

        self.remote = _remote_proxy

    def __call__(self, *args, **kwargs):
        raise TypeError("Remote functions cannot be called directly. Instead "
                        f"of running '{self._function_name}()', "
                        f"try '{self._function_name}.remote()'.")

    def options(self,
                args=None,
                kwargs=None,
                num_returns=None,
                num_cpus=None,
                num_gpus=None,
                memory=None,
                object_store_memory=None,
                accelerator_type=None,
                resources=None,
                max_retries=None,
                retry_exceptions=None,
                placement_group="default",
                placement_group_bundle_index=-1,
                placement_group_capture_child_tasks=None,
                runtime_env=None,
                override_environment_variables=None,
                name=""):
        """Configures and overrides the task invocation parameters.

        The arguments are the same as those that can be passed to
        :obj:`ray.remote`.

        Examples:

        .. code-block:: python

            @ray.remote(num_gpus=1, max_calls=1, num_returns=2)
            def f():
               return 1, 2
            # Task f will require 2 gpus instead of 1.
            g = f.options(num_gpus=2, max_calls=None)
        """

        func_cls = self

        class FuncWrapper:
            def remote(self, *args, **kwargs):
                return func_cls._remote(
                    args=args,
                    kwargs=kwargs,
                    num_returns=num_returns,
                    num_cpus=num_cpus,
                    num_gpus=num_gpus,
                    memory=memory,
                    object_store_memory=object_store_memory,
                    accelerator_type=accelerator_type,
                    resources=resources,
                    max_retries=max_retries,
                    retry_exceptions=retry_exceptions,
                    placement_group=placement_group,
                    placement_group_bundle_index=placement_group_bundle_index,
                    placement_group_capture_child_tasks=(
                        placement_group_capture_child_tasks),
                    runtime_env=runtime_env,
                    override_environment_variables=(
                        override_environment_variables),
                    name=name)

        return FuncWrapper()

    @_tracing_task_invocation
    def _remote(self,
                args=None,
                kwargs=None,
                num_returns=None,
                num_cpus=None,
                num_gpus=None,
                memory=None,
                object_store_memory=None,
                accelerator_type=None,
                resources=None,
                max_retries=None,
                retry_exceptions=None,
                placement_group="default",
                placement_group_bundle_index=-1,
                placement_group_capture_child_tasks=None,
                runtime_env=None,
                override_environment_variables=None,
                name=""):
        """Submit the remote function for execution."""
        if client_mode_should_convert():
            return client_mode_convert_function(
                self,
                args,
                kwargs,
                num_returns=num_returns,
                num_cpus=num_cpus,
                num_gpus=num_gpus,
                memory=memory,
                object_store_memory=object_store_memory,
                accelerator_type=accelerator_type,
                resources=resources,
                max_retries=max_retries,
                retry_exceptions=retry_exceptions,
                placement_group=placement_group,
                placement_group_bundle_index=placement_group_bundle_index,
                placement_group_capture_child_tasks=(
                    placement_group_capture_child_tasks),
                runtime_env=runtime_env,
                override_environment_variables=override_environment_variables,
                name=name)

        worker = ray.worker.global_worker
        worker.check_connected()

        # If this function was not exported in this session and job, we need to
        # export this function again, because the current GCS doesn't have it.
        if not self._is_cross_language and \
                self._last_export_session_and_job != \
                worker.current_session_and_job:
            # There is an interesting question here. If the remote function is
            # used by a subsequent driver (in the same script), should the
            # second driver pickle the function again? If yes, then the remote
            # function definition can differ in the second driver (e.g., if
            # variables in its closure have changed). We probably want the
            # behavior of the remote function in the second driver to be
            # independent of whether or not the function was invoked by the
            # first driver. This is an argument for repickling the function,
            # which we do here.
            self._pickled_function = pickle.dumps(self._function)
            self._function_descriptor = PythonFunctionDescriptor.from_function(
                self._function, self._uuid)

            self._last_export_session_and_job = worker.current_session_and_job
            worker.function_actor_manager.export(self)

        kwargs = {} if kwargs is None else kwargs
        args = [] if args is None else args

        if num_returns is None:
            num_returns = self._num_returns
        if max_retries is None:
            max_retries = self._max_retries
        if retry_exceptions is None:
            retry_exceptions = self._retry_exceptions

        if placement_group_capture_child_tasks is None:
            placement_group_capture_child_tasks = (
                worker.should_capture_child_tasks_in_placement_group)

        if placement_group == "default":
            if placement_group_capture_child_tasks:
                placement_group = get_current_placement_group()
            else:
                placement_group = PlacementGroup.empty()

        if not placement_group:
            placement_group = PlacementGroup.empty()

        check_placement_group_index(placement_group,
                                    placement_group_bundle_index)

        resources = ray._private.utils.resources_from_resource_arguments(
            self._num_cpus, self._num_gpus, self._memory,
            self._object_store_memory, self._resources, self._accelerator_type,
            num_cpus, num_gpus, memory, object_store_memory, resources,
            accelerator_type)

        if runtime_env is None:
            runtime_env = self._runtime_env

        job_runtime_env = worker.core_worker.get_current_runtime_env_dict()
        runtime_env_dict = runtime_support.override_task_or_actor_runtime_env(
            runtime_env, job_runtime_env)

        if override_environment_variables:
            logger.warning("override_environment_variables is deprecated and "
                           "will be removed in Ray 1.6.  Please use "
                           ".options(runtime_env={'env_vars': {...}}).remote()"
                           "instead.")

        def invocation(args, kwargs):
            if self._is_cross_language:
                list_args = cross_language.format_args(worker, args, kwargs)
            elif not args and not kwargs and not self._function_signature:
                list_args = []
            else:
                list_args = ray._private.signature.flatten_args(
                    self._function_signature, args, kwargs)

            if worker.mode == ray.worker.LOCAL_MODE:
                assert not self._is_cross_language, \
                    "Cross language remote function " \
                    "cannot be executed locally."
            object_refs = worker.core_worker.submit_task(
                self._language,
                self._function_descriptor,
                list_args,
                name,
                num_returns,
                resources,
                max_retries,
                retry_exceptions,
                placement_group.id,
                placement_group_bundle_index,
                placement_group_capture_child_tasks,
                worker.debugger_breakpoint,
                runtime_env_dict,
                override_environment_variables=override_environment_variables
                or dict())
            # Reset worker's debug context from the last "remote" command
            # (which applies only to this .remote call).
            worker.debugger_breakpoint = b""
            if len(object_refs) == 1:
                return object_refs[0]
            elif len(object_refs) > 1:
                return object_refs

        if self._decorator is not None:
            invocation = self._decorator(invocation)

        return invocation(args, kwargs)
