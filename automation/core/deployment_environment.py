"""Deployment-environment helpers shared by generators and deploy preflight.

``FabricIntent.environment`` is either ``containerlab`` (lab / twin target) or
``physical`` (hardware target). Generators branch on it so the same intent
produces lab-appropriate NodeProfiles, Init CRs, Ansible metadata, and clab
headers instead of treating every design as a containerlab deployment.
"""

from __future__ import annotations

from typing import Literal

from automation.eda_models.bootstrap import InitMgmt, InitSpec
from automation.eda_models.core import NodeProfileImages, NodeProfileSpec

Environment = Literal["containerlab", "physical"]

CONTAINERLAB: Environment = "containerlab"
PHYSICAL: Environment = "physical"

# gNMI port used by containerlab SR Linux nodes onboarded through EDA.
CLAB_GNMI_PORT = 57410
# Default gNMI port on hardware and in the NodeProfile model default.
HW_GNMI_PORT = 57400

SRL_IMAGE_BASE = "ghcr.io/nokia/srlinux"


def default_node_profile_name(environment: str, version: str) -> str:
    """Return the default NodeProfile CR name when ``eda.node_profile`` is unset."""
    if environment == PHYSICAL:
        return f"srlinux-hw-{version}"
    return f"clab-srlinux-{version}"


def build_init_spec(environment: str) -> InitSpec:
    """Build the Init CR spec for the deployment environment."""
    if environment == CONTAINERLAB:
        return InitSpec(
            commit_save=True,
            mgmt=InitMgmt(ipv4_dhcp=True, ipv6_dhcp=True),
        )
    return InitSpec(commit_save=True)


def build_node_profile_spec(
    environment: str,
    version: str,
    *,
    username: str,
    password: str,
) -> NodeProfileSpec:
    """Build a NodeProfile spec tuned for lab onboarding or hardware."""
    ver_escaped = version.replace(".", "\\.")
    yang = (
        f"https://eda-asvr.eda-system.svc/eda-system/schemaprofiles/"
        f"srlinux-ghcr-{version}/srlinux-{version}.zip"
    )
    llm_db = (
        f"https://eda-asvr.eda-system.svc/eda-system/llm-dbs/"
        f"llm-db-srlinux-ghcr-{version}/llm-embeddings-srl-{version.replace('.', '-')}.tar.gz"
    )
    common = dict(
        images=[
            NodeProfileImages(
                image=f"srlimages/srlinux-{version}-bin/srlinux.bin",
                image_md5=f"srlimages/srlinux-{version}-md5/srlinux.md5",
            )
        ],
        llm_db=llm_db,
        node_user=username,
        onboarding_username=username,
        onboarding_password=password,
        operating_system="srl",
        version=version,
        version_match=f"v{ver_escaped}.*",
        version_path=".system.information.version",
        yang=yang,
    )
    if environment == PHYSICAL:
        return NodeProfileSpec(
            **common,
            annotate=False,
            port=HW_GNMI_PORT,
        )
    return NodeProfileSpec(
        **common,
        annotate=True,
        container_image=f"{SRL_IMAGE_BASE}:{version}",
        port=CLAB_GNMI_PORT,
    )
