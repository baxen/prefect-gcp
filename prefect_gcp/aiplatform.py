"""
<span class="badge-api experimental"/>

Integrations with Google AI Platform.

Note this module is experimental. The intefaces within may change without notice.

Examples:

    Run a job using Vertex AI Custom Training:
    ```python
    from prefect_gcp.credentials import GcpCredentials
    from prefect_gcp.aiplatform import VertexAICustomTrainingJob

    gcp_credentials = GcpCredentials.load("BLOCK_NAME")
    job = VertexAICustomTrainingJob(
        region="us-east1",
        image="us-docker.pkg.dev/cloudrun/container/job:latest",
        gcp_credentials=gcp_credentials,
    )
    job.run()
    ```

    Run a job that runs the command `echo hello world` using Google Cloud Run Jobs:
    ```python
    from prefect_gcp.credentials import GcpCredentials
    from prefect_gcp.aiplatform import VertexAICustomTrainingJob

    gcp_credentials = GcpCredentials.load("BLOCK_NAME")
    job = VertexAICustomTrainingJob(
        command=["echo", "hello world"],
        region="us-east1",
        image="us-docker.pkg.dev/cloudrun/container/job:latest",
        gcp_credentials=gcp_credentials,
    )
    job.run()
    ```

    Preview job specs:
    ```python
    from prefect_gcp.credentials import GcpCredentials
    from prefect_gcp.aiplatform import VertexAICustomTrainingJob

    gcp_credentials = GcpCredentials.load("BLOCK_NAME")
    job = VertexAICustomTrainingJob(
        command=["echo", "hello world"],
        region="us-east1",
        image="us-docker.pkg.dev/cloudrun/container/job:latest",
        gcp_credentials=gcp_credentials,
    )
    job.preview()
    ```
"""

import datetime
import time
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from anyio.abc import TaskStatus
from prefect.exceptions import InfrastructureNotFound
from prefect.infrastructure import Infrastructure, InfrastructureResult
from prefect.utilities.asyncutils import run_sync_in_worker_thread, sync_compatible
from pydantic import Field
from typing_extensions import Literal

# to prevent "Failed to load collection" from surfacing
# if google-cloud-aiplatform is not installed
try:
    from google.api_core.client_options import ClientOptions
    from google.cloud.aiplatform.gapic import JobServiceClient
    from google.cloud.aiplatform_v1.types.custom_job import (
        ContainerSpec,
        CustomJob,
        CustomJobSpec,
        Scheduling,
        WorkerPoolSpec,
    )
    from google.cloud.aiplatform_v1.types.job_service import CancelCustomJobRequest
    from google.cloud.aiplatform_v1.types.job_state import JobState
    from google.cloud.aiplatform_v1.types.machine_resources import DiskSpec, MachineSpec
    from google.protobuf.duration_pb2 import Duration
except ModuleNotFoundError:
    pass

from prefect_gcp.credentials import GcpCredentials


class VertexAICustomTrainingJobResult(InfrastructureResult):
    """Result from a Vertex AI custom training job."""


class VertexAICustomTrainingJob(Infrastructure):
    """
    Infrastructure block used to run Vertex AI custom training jobs.
    """

    _block_type_name = "Vertex AI Custom Training Job"
    _block_type_slug = "vertex-ai-custom-training-job"
    _logo_url = "https://images.ctfassets.net/gm98wzqotmnx/4CD4wwbiIKPkZDt4U3TEuW/c112fe85653da054b6d5334ef662bec4/gcp.png?h=250"  # noqa
    _documentation_url = "https://prefecthq.github.io/prefect-gcp/aiplatform/#prefect_gcp.aiplatform.VertexAICustomTrainingJob"  # noqa: E501

    type: Literal["vertex-ai-custom-training-job"] = Field(
        "vertex-ai-custom-training-job", description="The slug for this task type."
    )

    gcp_credentials: GcpCredentials = Field(
        default_factory=GcpCredentials,
        description=(
            "GCP credentials to use when running the configured Vertex AI custom "
            "training job. If not provided, credentials will be inferred from the "
            "environment. See `GcpCredentials` for details."
        ),
    )
    region: str = Field(
        default=...,
        description="The region where the Vertex AI custom training job resides.",
    )
    image: str = Field(
        default=...,
        title="Image Name",
        description=(
            "The image to use for a new Vertex AI custom training job. This value must "
            "refer to an image within either Google Container Registry "
            "or Google Artifact Registry, like `gcr.io/<project_name>/<repo>/`."
        ),
    )
    env: Dict[str, str] = Field(
        default_factory=dict,
        title="Environment Variables",
        description="Environment variables to be passed to your Cloud Run Job.",
    )
    machine_type: str = Field(
        default="n1-standard-4",
        description="The machine type to use for the run, which controls the available "
        "CPU and memory.",
    )
    accelerator_type: Optional[str] = Field(
        default=None, description="The type of accelerator to attach to the machine."
    )
    accelerator_count: Optional[int] = Field(
        default=None, description="The number of accelerators to attach to the machine."
    )
    boot_disk_type: str = Field(
        default="pd-ssd",
        title="Boot Disk Type",
        description="The type of boot disk to attach to the machine.",
    )
    boot_disk_size_gb: int = Field(
        default=100,
        title="Boot Disk Size",
        description="The size of the boot disk to attach to the machine, in gigabytes.",
    )
    maximum_run_time: datetime.timedelta = Field(
        default=datetime.timedelta(days=7), description="The maximum job running time."
    )
    network: Optional[str] = Field(
        default=None,
        description="The full name of the Compute Engine network"
        "to which the Job should be peered. Private services access must "
        "already be configured for the network. If left unspecified, the job "
        "is not peered with any network.",
    )
    reserved_ip_ranges: Optional[List[str]] = Field(
        default=None,
        description="A list of names for the reserved ip ranges under the VPC "
        "network that can be used for this job. If set, we will deploy the job "
        "within the provided ip ranges. Otherwise, the job will be deployed to "
        "any ip ranges under the provided VPC network.",
    )
    service_account: Optional[str] = Field(
        default=None,
        description=(
            "Specifies the service account to use "
            "as the run-as account in Vertex AI. The agent submitting jobs must have "
            "act-as permission on this run-as account. If unspecified, the AI "
            "Platform Custom Code Service Agent for the CustomJob's project is "
            "used. Takes precedence over the service account found in gcp_credentials, "
            "and required if a service account cannot be detected in gcp_credentials."
        ),
    )

    job_watch_poll_interval: float = Field(
        default=5.0,
        description=(
            "The amount of time to wait between GCP API calls while monitoring the "
            "state of a Vertex AI Job."
        ),
    )

    @property
    def job_name(self):
        """
        The name can be up to 128 characters long and can be consist of any UTF-8 characters. Reference:
        https://cloud.google.com/python/docs/reference/aiplatform/latest/google.cloud.aiplatform.CustomJob#google_cloud_aiplatform_CustomJob_display_name
        """  # noqa
        try:
            repo_name = self.image.split("/")[2]  # `gcr.io/<project_name>/<repo>/`"
        except IndexError:
            raise ValueError(
                "The provided image must be from either Google Container Registry "
                "or Google Artifact Registry"
            )

        unique_suffix = uuid4().hex
        job_name = f"{repo_name}-{unique_suffix}"
        return job_name

    def preview(self) -> str:
        """Generate a preview of the job definition that will be sent to GCP."""
        job_spec = self._build_job_spec()
        custom_job = CustomJob(display_name=self.job_name, job_spec=job_spec)
        return str(custom_job)  # outputs a json string

    def _build_job_spec(self) -> "CustomJobSpec":
        """
        Builds a job spec by gathering details.
        """
        # gather worker pool spec
        env_list = [
            {"name": name, "value": value}
            for name, value in {
                **self._base_environment(),
                **self.env,
            }.items()
        ]
        container_spec = ContainerSpec(
            image_uri=self.image, command=self.command, args=[], env=env_list
        )
        machine_spec = MachineSpec(
            machine_type=self.machine_type,
            accelerator_type=self.accelerator_type,
            accelerator_count=self.accelerator_count,
        )
        worker_pool_spec = WorkerPoolSpec(
            container_spec=container_spec,
            machine_spec=machine_spec,
            replica_count=1,
            disk_spec=DiskSpec(
                boot_disk_type=self.boot_disk_type,
                boot_disk_size_gb=self.boot_disk_size_gb,
            ),
        )
        # look for service account
        service_account = (
            self.service_account or self.gcp_credentials._service_account_email
        )
        if service_account is None:
            raise ValueError(
                "A service account is required for the Vertex job. "
                "A service account could not be detected in the attached credentials; "
                "please set a service account explicitly, e.g. "
                '`VertexAICustomTrainingJob(service_acount="...")`'
            )

        # build custom job specs
        timeout = Duration().FromTimedelta(td=self.maximum_run_time)
        scheduling = Scheduling(timeout=timeout)
        job_spec = CustomJobSpec(
            worker_pool_specs=[worker_pool_spec],
            service_account=service_account,
            scheduling=scheduling,
            network=self.network,
            reserved_ip_ranges=self.reserved_ip_ranges,
        )
        return job_spec

    async def _create_and_begin_job(
        self, job_spec: "CustomJobSpec", job_service_client: "JobServiceClient"
    ) -> "CustomJob":
        """
        Builds a custom job and begins running it.
        """
        # create custom job
        custom_job = CustomJob(display_name=self.job_name, job_spec=job_spec)

        # run job
        self.logger.info(
            f"{self._log_prefix}: Job {self.job_name!r} starting to run "
            f"the command {' '.join(self.command)!r} in region "
            f"{self.region!r} using image {self.image!r}"
        )

        project = self.gcp_credentials.project
        resource_name = f"projects/{project}/locations/{self.region}"
        custom_job_run = await run_sync_in_worker_thread(
            job_service_client.create_custom_job,
            parent=resource_name,
            custom_job=custom_job,
        )

        self.logger.info(
            f"{self._log_prefix}: Job {self.job_name!r} has successfully started; "
            f"the full job name is {custom_job_run.name!r}"
        )

        return custom_job_run

    async def _watch_job_run(
        self,
        full_job_name: str,  # different from self.job_name
        job_service_client: "JobServiceClient",
        current_state: "JobState",
        until_states: Tuple["JobState"],
        timeout: int = None,
    ) -> "CustomJob":
        """
        Polls job run to see if status changed.
        """
        state = JobState.JOB_STATE_UNSPECIFIED
        last_state = current_state
        t0 = time.time()

        while state not in until_states:
            job_run = await run_sync_in_worker_thread(
                job_service_client.get_custom_job,
                name=full_job_name,
            )
            state = job_run.state
            if state != last_state:
                state_label = (
                    state.name.replace("_", " ")
                    .lower()
                    .replace("state", "state is now:")
                )
                # results in "New job state is now: succeeded"
                self.logger.info(
                    f"{self._log_prefix}: {self.job_name} has new {state_label}"
                )
                last_state = state
            else:
                # Intermittently, the job will not be described. We want to respect the
                # watch timeout though.
                self.logger.debug(f"{self._log_prefix}: Job not found.")

            elapsed_time = time.time() - t0
            if timeout is not None and elapsed_time > timeout:
                raise RuntimeError(
                    f"Timed out after {elapsed_time}s while watching job for states "
                    "{until_states!r}"
                )
            time.sleep(self.job_watch_poll_interval)

        return job_run

    @sync_compatible
    async def run(
        self, task_status: Optional["TaskStatus"] = None
    ) -> VertexAICustomTrainingJobResult:
        """
        Run the configured task on VertexAI.

        Args:
            task_status: An optional `TaskStatus` to update when the container starts.

        Returns:
            The `VertexAICustomTrainingJobResult`.
        """
        client_options = ClientOptions(
            api_endpoint=f"{self.region}-aiplatform.googleapis.com"
        )

        job_spec = self._build_job_spec()
        with self.gcp_credentials.get_job_service_client(
            client_options=client_options
        ) as job_service_client:
            job_run = await self._create_and_begin_job(job_spec, job_service_client)

            if task_status:
                task_status.started(self.job_name)

            final_job_run = await self._watch_job_run(
                full_job_name=job_run.name,
                job_service_client=job_service_client,
                current_state=job_run.state,
                until_states=(
                    JobState.JOB_STATE_SUCCEEDED,
                    JobState.JOB_STATE_FAILED,
                    JobState.JOB_STATE_CANCELLED,
                    JobState.JOB_STATE_EXPIRED,
                ),
                timeout=self.maximum_run_time.total_seconds(),
            )

        error_msg = final_job_run.error.message
        if error_msg:
            raise RuntimeError(f"{self._log_prefix}: {error_msg}")

        status_code = 0 if final_job_run.state == JobState.JOB_STATE_SUCCEEDED else 1
        return VertexAICustomTrainingJobResult(
            identifier=final_job_run.display_name, status_code=status_code
        )

    @sync_compatible
    async def kill(self, identifier: str, grace_seconds: int = 30) -> None:
        """
        Kill a job running Cloud Run.

        Args:
            identifier: The Vertex AI full job name, formatted like
                "projects/{project}/locations/{location}/customJobs/{custom_job}".

        Returns:
            The `VertexAICustomTrainingJobResult`.
        """
        client_options = ClientOptions(
            api_endpoint=f"{self.region}-aiplatform.googleapis.com"
        )
        with self.gcp_credentials.get_job_service_client(
            client_options=client_options
        ) as job_service_client:
            await run_sync_in_worker_thread(
                self._kill_job,
                job_service_client=job_service_client,
                full_job_name=identifier,
            )
            self.logger.info(f"Requested to cancel {identifier}...")

    def _kill_job(
        self, job_service_client: "JobServiceClient", full_job_name: str
    ) -> None:
        """
        Thin wrapper around Job.delete, wrapping a try/except since
        Job is an independent class that doesn't have knowledge of
        CloudRunJob and its associated logic.
        """
        cancel_custom_job_request = CancelCustomJobRequest(name=full_job_name)
        try:
            job_service_client.cancel_custom_job(
                request=cancel_custom_job_request,
            )
        except Exception as exc:
            if "does not exist" in str(exc):
                raise InfrastructureNotFound(
                    f"Cannot stop Vertex AI job; the job name {full_job_name!r} "
                    "could not be found."
                ) from exc
            raise

    @property
    def _log_prefix(self) -> str:
        """
        Internal property for generating a prefix for logs where `name` may be null
        """
        if self.name is not None:
            return f"VertexAICustomTrainingJob {self.name!r}"
        else:
            return "VertexAICustomTrainingJob"
