# ---
# jupyter:
#   jupytext:
#     cell_metadata_filter: -all
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.16.2
#   kernelspec:
#     display_name: pyrit-311
#     language: python
#     name: python3
# ---

# %% [markdown]
# ## Generating GCG Suffixes Using Azure Machine Learning

# %% [markdown]
# This notebook shows how to generate GCG suffixes using Azure Machine Learning (AML), which consists of three main steps:
# 1. Connect to an Azure Machine Learning (AML) workspace.
# 2. Create AML Environment with the Python dependencies.
# 3. Submit a training job to AML.

# %% [markdown]
# ### Connect to Azure Machine Learning Workspace

# %% [markdown]
# The [workspace](https://docs.microsoft.com/en-us/azure/machine-learning/concept-workspace) is the top-level resource for Azure Machine Learning (AML), providing a centralized place to work with all the artifacts you create when using AML. In this section, we will connect to the workspace in which the job will be run.
#
# To connect to a workspace, we need identifier parameters - a subscription, resource group and workspace name. We will use these details in the `MLClient` from `azure.ai.ml` to get a handle to the required AML workspace. We use the [default Azure authentication](https://docs.microsoft.com/en-us/python/api/azure-identity/azure.identity.defaultazurecredential?view=azure-python) for this tutorial.

# %%
import os
from pyrit.common import default_values

default_values.load_default_env()

# Enter details of your AML workspace
subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID")
resource_group = os.environ.get("AZURE_RESOURCE_GROUP")
workspace = os.environ.get("AZURE_ML_WORKSPACE_NAME")

# %%
from azure.ai.ml import MLClient
from azure.identity import DefaultAzureCredential

# Get a handle to the workspace
ml_client = MLClient(DefaultAzureCredential(), subscription_id, resource_group, workspace)

# %% [markdown]
# ### Create AML Environment

# %% [markdown]
# To install the dependencies needed to run GCG, we create an AML environment from a [Dockerfile](../../../pyrit/auxiliary_attacks/gcg/src/Dockerfile).

# %%
from azure.ai.ml.entities import Environment, BuildContext

# Configure the AML environment with path to Dockerfile and dependencies
env_docker_context = Environment(
    build=BuildContext(path="../../../pyrit/auxiliary_attacks/gcg/src"),
    name="pyrit",
    description="PyRIT environment created from a Docker context.",
)

# Create or update the AML environment
ml_client.environments.create_or_update(env_docker_context)


# %% [markdown]
# ### Submit Training Job to AML

# %% [markdown]
# Finally, we configure the command to run the GCG algorithm. The entry file for the algorithm is []`run.py`](../../../pyrit/auxiliary_attacks/gcg/experiments/run.py), which takes several command line arguments, as shown below. We also have to specify the compute `instance_type` to run the algorithm on. In our experience, a GPU instance with at least 32GB of vRAM is required. As shown below, we use Standard_ND40rs_v2.

# %%
from azure.ai.ml import command
from azure.ai.ml.entities import JobResourceConfiguration

# Configure the command
job = command(
    code="./../../..",  # local path where the code is stored
    command="cd pyrit/auxiliary_attacks/gcg/experiments && python run.py --model_name ${{inputs.model_name}} --setup ${{inputs.setup}} --n_train_data ${{inputs.n_train_data}} --n_test_data ${{inputs.n_test_data}} --n_steps ${{inputs.n_steps}} --batch_size ${{inputs.batch_size}}",
    inputs={
        "model_name": "phi_3_mini",
        "setup": "multiple",
        "n_train_data": 25,
        "n_test_data": 0,
        "n_steps": 500,
        "batch_size": 256,
    },
    environment=f"{env_docker_context.name}:{env_docker_context.version}",
    environment_variables={"HF_TOKEN": os.environ["HF_TOKEN"]},
    display_name="suffix_generation",
    description="Generate a suffix for attacking LLMs.",
    resources=JobResourceConfiguration(
        instance_type="Standard_ND40rs_v2",
        instance_count=1,
    ),
)

# %%
# Submit the command
returned_job = ml_client.create_or_update(job)