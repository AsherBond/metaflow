import json
import os


from metaflow import current
from metaflow.decorators import StepDecorator
from metaflow.events import Trigger
from metaflow.metadata_provider import MetaDatum
from metaflow.graph import FlowGraph
from metaflow.flowspec import FlowSpec
from .argo_events import ArgoEvent


class ArgoWorkflowsInternalDecorator(StepDecorator):
    name = "argo_workflows_internal"

    defaults = {"auto-emit-argo-events": True}

    def task_pre_step(
        self,
        step_name,
        task_datastore,
        metadata,
        run_id,
        task_id,
        flow,
        graph,
        retry_count,
        max_user_code_retries,
        ubf_context,
        inputs,
    ):
        self.task_id = task_id
        self.run_id = run_id

        triggers = []
        # Expose event triggering metadata through current singleton
        for key, payload in os.environ.items():
            if key.startswith("METAFLOW_ARGO_EVENT_PAYLOAD_"):
                if payload != "null":  # Argo-Workflow's None
                    try:
                        payload = json.loads(payload)
                    except (TypeError, ValueError):
                        # There could be arbitrary events that Metaflow doesn't know of
                        payload = {}
                    triggers.append(
                        {
                            "timestamp": payload.get("timestamp"),
                            "id": payload.get("id"),
                            "name": payload.get("name"),  # will exist since filter
                            "type": key[len("METAFLOW_ARGO_EVENT_PAYLOAD_") :].split(
                                "_", 1
                            )[
                                0
                            ],  # infer type from env var key
                            # Add more event metadata here in the future
                        }
                    )

        meta = {}
        if triggers:
            # Enable current.trigger
            current._update_env({"trigger": Trigger(triggers)})
            # Luckily there aren't many events for us to be concerned about the
            # size of the metadata field yet! However we don't really need this
            # metadata outside of the start step so we can save a few bytes in the
            # db.
            if step_name == "start":
                meta["execution-triggers"] = json.dumps(triggers)

        meta["argo-workflow-template"] = os.environ["ARGO_WORKFLOW_TEMPLATE"]
        meta["argo-workflow-name"] = os.environ["ARGO_WORKFLOW_NAME"]
        meta["argo-workflow-namespace"] = os.environ["ARGO_WORKFLOW_NAMESPACE"]
        meta["auto-emit-argo-events"] = self.attributes["auto-emit-argo-events"]
        meta["argo-workflow-template-owner"] = os.environ["METAFLOW_OWNER"]
        entries = [
            MetaDatum(
                field=k, value=v, type=k, tags=["attempt_id:{0}".format(retry_count)]
            )
            for k, v in meta.items()
        ]
        # Register book-keeping metadata for debugging.
        metadata.register_metadata(run_id, step_name, task_id, entries)

    def task_finished(
        self,
        step_name,
        flow: FlowSpec,
        graph: FlowGraph,
        is_task_ok,
        retry_count,
        max_user_code_retries,
    ):
        if not is_task_ok:
            # The task finished with an exception - execution won't
            # continue so no need to do anything here.
            return

        # For `foreach`s, we need to dump the cardinality of the fanout
        # into a file so that Argo Workflows can properly configure
        # the subsequent fanout task via an Output parameter
        #
        # Docker and PNS workflow executors can get output parameters from the base
        # layer (e.g. /tmp), but the Kubelet nor the K8SAPI nor the emissary executors
        # can. It is also unlikely we can get output parameters from the base layer if
        # we run pods with a security context. We work around this constraint by
        # mounting an emptyDir volume.
        if graph[step_name].type == "foreach":
            if graph[step_name].parallel_foreach:
                # If a node is marked as a `parallel_foreach`, pass down the value of
                # `num_parallel` to the subsequent steps.
                with open("/mnt/out/num_parallel", "w") as f:
                    json.dump(flow._parallel_ubf_iter.num_parallel, f)
                # Set splits to 1 since parallelism is handled by JobSet.
                flow._foreach_num_splits = 1
                with open("/mnt/out/task_id_entropy", "w") as file:
                    import uuid

                    file.write(uuid.uuid4().hex[:6])

            with open("/mnt/out/splits", "w") as file:
                json.dump(list(range(flow._foreach_num_splits)), file)
            with open("/mnt/out/split_cardinality", "w") as file:
                json.dump(flow._foreach_num_splits, file)

        # For steps that have a `@parallel` decorator set to them, we will be relying on Jobsets
        # to run the task. In this case, we cannot set anything in the
        # `/mnt/out` directory, since such form of output mounts are not available to Jobset executions.
        if not graph[step_name].parallel_step:
            # Unfortunately, we can't always use pod names as task-ids since the pod names
            # are not static across retries. We write the task-id to a file that is read
            # by the next task here.
            with open("/mnt/out/task_id", "w") as file:
                file.write(self.task_id)

        # Emit Argo Events given that the flow has succeeded. Given that we only
        # emit events when the task succeeds, we can piggy back on this decorator
        # hook which is guaranteed to execute only after rest of the task has
        # finished execution.

        if self.attributes["auto-emit-argo-events"]:
            # Event name is set to metaflow.project.branch.step so that users can
            # place explicit dependencies on namespaced events. Also, argo events
            # sensors don't allow for filtering against absent fields - which limits
            # our ability to subset non-project namespaced events.
            # TODO: Check length limits for fields in Argo Events
            event = ArgoEvent(
                name="metaflow.%s.%s"
                % (current.get("project_flow_name", flow.name), step_name)
            )
            # There should only be one event generated even when the task is retried.
            # Take care to only add to the list and not modify existing values.
            event.add_to_payload("id", current.pathspec)
            event.add_to_payload("pathspec", current.pathspec)
            event.add_to_payload("flow_name", flow.name)
            event.add_to_payload("run_id", self.run_id)
            event.add_to_payload("step_name", step_name)
            event.add_to_payload("task_id", self.task_id)
            # Add @project decorator related fields. These are used to subset
            # @trigger_on_finish related filters.
            for key in (
                "project_name",
                "branch_name",
                "is_user_branch",
                "is_production",
                "project_flow_name",
            ):
                if current.get(key):
                    event.add_to_payload(key, current.get(key))
            # Add more fields here...
            event.add_to_payload("auto-generated-by-metaflow", True)
            # Keep in mind that any errors raised here will fail the run but the task
            # will still be marked as success. That's why we explicitly swallow any
            # errors and instead print them to std.err.
            event.safe_publish(ignore_errors=True)
