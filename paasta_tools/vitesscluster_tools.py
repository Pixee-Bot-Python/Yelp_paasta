import copy
import logging
from typing import Dict
from typing import List
from typing import Mapping
from typing import Optional

import service_configuration_lib

from paasta_tools.kubernetes_tools import sanitised_cr_name
from paasta_tools.long_running_service_tools import LongRunningServiceConfig
from paasta_tools.long_running_service_tools import LongRunningServiceConfigDict
from paasta_tools.utils import BranchDictV2
from paasta_tools.utils import deep_merge_dictionaries
from paasta_tools.utils import DEFAULT_SOA_DIR
from paasta_tools.utils import load_service_instance_config
from paasta_tools.utils import load_system_paasta_config
from paasta_tools.utils import load_v2_deployments_json

KUBERNETES_NAMESPACE = "paasta-vitessclusters"
# Image variables
IMAGE_TAG = "v16.0.3"
VTCTLD_IMAGE = f"docker-paasta.yelpcorp.com:443/vitess_base:{IMAGE_TAG}"
VT_GATE_IMAGE = f"docker-paasta.yelpcorp.com:443/vitess_base:{IMAGE_TAG}"
VT_TABLET_IMAGE = f"docker-paasta.yelpcorp.com:443/vitess_base:{IMAGE_TAG}"
VT_ADMIN_IMAGE = f"docker-dev.yelpcorp.com/vtadmin:{IMAGE_TAG}"


# Global variables
TOPO_IMPLEMENTATION = "zk2"
TOPO_GLOBAL_ROOT = "/vitess-paasta/global"
SOURCE_DB_HOST = "169.254.255.254"
TABLET_TYPES = ["primary", "migration"]
WEB_PORT = "15000"
GRPC_PORT = "15999"


# Environment variables
VTCTLD_EXTRA_ENV = [
    {
        "name": "WEB_PORT",
        "value": WEB_PORT,
    },
    {
        "name": "GRPC_PORT",
        "value": GRPC_PORT,
    },
    {
        "name": "TOPOLOGY_FLAGS",
        "value": "",
    },
]

VTTABLET_EXTRA_ENV = [
    {
        "name": "CELL_TOPOLOGY_SERVERS",
        "value": "",
    },
    {
        "name": "SHARD",
        "value": "0",
    },
    {
        "name": "DB",
        "value": "",
    },
    {
        "name": "EXTERNAL_DB",
        "value": "1",
    },
    {
        "name": "KEYSPACE",
        "value": "",
    },
    {
        "name": "ROLE",
        "value": "rdonly",
    },
    {
        "name": "WEB_PORT",
        "value": WEB_PORT,
    },
    {
        "name": "GRPC_PORT",
        "value": GRPC_PORT,
    },
    {
        "name": "TOPOLOGY_FLAGS",
        "value": "",
    },
    {
        "name": "VAULT_ADDR",
        "value": "https://vault-dre.uswest1-devc.yelpcorp.com:8200",
    },
    {
        "name": "VAULT_ROLEID",
        "valueFrom": {
            "secretKeyRef": {
                "name": "paasta-vitessclusters-secret-vitess-k8s-vault-vttablet-approle-roleid",
                "key": "vault-vttablet-approle-roleid",
            }
        },
    },
    {
        "name": "VAULT_SECRETID",
        "valueFrom": {
            "secretKeyRef": {
                "name": "paasta-vitessclusters-secret-vitess-k8s-vault-vttablet-approle-secretid",
                "key": "vault-vttablet-approle-secretid",
            }
        },
    },
    {
        "name": "VAULT_CACERT",
        "value": "/etc/vault/all_cas/acm-privateca-uswest1-devc.crt",
    },
]

# Vault auth related variables
VTGATE_EXTRA_ENV = [
    {
        "name": "VAULT_ADDR",
        "value": "https://vault-dre.uswest1-devc.yelpcorp.com:8200",
    },
    {
        "name": "VAULT_ROLEID",
        "valueFrom": {
            "secretKeyRef": {
                "name": "paasta-vitessclusters-secret-vitess-k8s-vault-vtgate-approle-roleid",
                "key": "vault-vtgate-approle-roleid",
            }
        },
    },
    {
        "name": "VAULT_SECRETID",
        "valueFrom": {
            "secretKeyRef": {
                "name": "paasta-vitessclusters-secret-vitess-k8s-vault-vtgate-approle-secretid",
                "key": "vault-vtgate-approle-secretid",
            }
        },
    },
    {
        "name": "VAULT_CACERT",
        "value": "/etc/vault/all_cas/acm-privateca-uswest1-devc.crt",
    },
]


# Extra Flags
VTADMIN_EXTRA_FLAGS = {"grpc-allow-reflection": "true"}

VTCTLD_EXTRA_FLAGS = {
    "disable_active_reparents": "true",
    "security_policy": "read-only",
}

VTTABLET_EXTRA_FLAGS = {
    "log_err_stacks": "true",
    "grpc_max_message_size": "134217728",
    "init_tablet_type": "replica",
    "queryserver-config-schema-reload-time": "1800",
    "dba_pool_size": "4",
    "vreplication_heartbeat_update_interval": "60",
    "vreplication_tablet_type": "REPLICA",
    "keep_logs": "72h",
    "enable-lag-throttler": "true",
    "throttle_check_as_check_self": "true",
    "throttle_metrics_query": "",
    "throttle_metrics_threshold": "",
    "db_charset": "utf8mb4",
    "disable_active_reparents": "true",
}


def build_affinity_spec(paasta_pool):
    spec = {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": "yelp.com/pool",
                                "operator": "In",
                                "values": [paasta_pool],
                            }
                        ]
                    }
                ]
            }
        }
    }
    return spec


def build_extra_labels(paasta_pool, paasta_cluster):
    """
    Build extra labels to adhere to paasta contract
    """
    extra_labels = {
        "yelp.com/owner": "dre_mysql_working_hours",
        "paasta.yelp.com/cluster": paasta_cluster,
        "paasta.yelp.com/pool": paasta_pool,
    }
    return extra_labels


def build_extra_env(paasta_cluster):
    """
    Build extra env to adhere to paasta contract
    """
    extra_env = [
        {
            "name": "PAASTA_POD_IP",
            "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}},
        },
        {"name": "PAASTA_CLUSTER", "value": paasta_cluster},
    ]
    return extra_env


def build_cell_config(cell, paasta_pool, paasta_cluster, region):
    """
    Build vtgate config
    """
    config = {
        "name": cell,
        "gateway": {
            "extraFlags": {
                "mysql_auth_server_impl": "vault",
                "mysql_auth_vault_addr": f"https://vault-dre.{region}.yelpcorp.com:8200",
                "mysql_auth_vault_path": "secrets/vitess/vt-gate/vttablet_credentials.json",
                "mysql_auth_vault_tls_ca": f"/etc/vault/all_cas/acm-privateca-{region}.crt",
                "mysql_auth_vault_ttl": "60s",
            },
            "affinity": build_affinity_spec(paasta_pool),
            "extraLabels": build_extra_labels(paasta_pool, paasta_cluster),
            "extraEnv": build_extra_env(paasta_cluster),
            "replicas": 1,
            "resources": {
                "requests": {
                    "cpu": "100m",
                    "memory": "256Mi",
                },
                "limits": {"memory": "256Mi"},
            },
        },
    }
    vtgate_extra_env = copy.deepcopy(VTGATE_EXTRA_ENV)
    for env in vtgate_extra_env:
        if env["name"] == "VAULT_ADDR":
            env["value"] = f"https://vault-dre.{region}.yelpcorp.com:8200"
        if env["name"] == "VAULT_CACERT":
            env["value"] = f"/etc/vault/all_cas/acm-privateca-{region}.crt"
        config["gateway"]["extraEnv"].append(env)
    return config


def build_vitess_dashboard_config(cells, paasta_pool, paasta_cluster, zk_address):
    """
    Build vtctld config
    """
    config = {
        "cells": cells,
        "affinity": build_affinity_spec(paasta_pool),
        "extraLabels": build_extra_labels(paasta_pool, paasta_cluster),
        "extraEnv": build_extra_env(paasta_cluster),
        "extraFlags": VTCTLD_EXTRA_FLAGS,
        "replicas": 1,
        "resources": {
            "requests": {
                "cpu": "100m",
                "memory": "128Mi",
            },
            "limits": {"memory": "128Mi"},
        },
    }
    vtctld_extra_env = copy.deepcopy(VTCTLD_EXTRA_ENV)
    for env in vtctld_extra_env:
        if env["name"] == "TOPOLOGY_FLAGS":
            env[
                "value"
            ] = f"--topo_implementation {TOPO_IMPLEMENTATION} --topo_global_server_address ${zk_address} --topo_global_root {TOPO_GLOBAL_ROOT}"
        config["extraEnv"].append(env)

    return config


def build_vt_admin_config(cells, paasta_pool, paasta_cluster):
    """
    Build vtadmin config
    """
    config = {
        "cells": cells,
        "apiAddresses": ["http://localhost:15000"],
        "affinity": build_affinity_spec(paasta_pool),
        "extraLabels": build_extra_labels(paasta_pool, paasta_cluster),
        "extraFlags": VTADMIN_EXTRA_FLAGS,
        "extraEnv": build_extra_env(paasta_cluster),
        "replicas": 1,
        "readOnly": False,
        "apiResources": {
            "requests": {
                "cpu": "100m",
                "memory": "128Mi",
            },
            "limits": {"memory": "128Mi"},
        },
        "webResources": {
            "requests": {
                "cpu": "100m",
                "memory": "128Mi",
            },
            "limits": {"memory": "128Mi"},
        },
    }
    return config


def build_tablet_pool_config(
    cell,
    db_name,
    keyspace,
    port,
    paasta_pool,
    paasta_cluster,
    zk_address,
    throttle_query_table,
    throttle_metrics_threshold,
    tablet_type,
    region,
):
    """
    Build vttablet config
    """
    vttablet_extra_flags = VTTABLET_EXTRA_FLAGS.copy()
    vttablet_extra_flags[
        "throttle_metrics_query"
    ] = f"select max_replication_delay from max_mysql_replication_delay.{throttle_query_table};"
    vttablet_extra_flags["throttle_metrics_threshold"] = throttle_metrics_threshold
    vttablet_extra_flags["enforce-tableacl-config"] = "true"
    vttablet_extra_flags[
        "table-acl-config"
    ] = f"/etc/vitess_keyspace_acls/acls_for_{db_name}.json"
    vttablet_extra_flags["table-acl-config-reload-interval"] = "60s"
    vttablet_extra_flags["queryserver-config-strict-table-acl"] = "true"
    vttablet_extra_flags["db-credentials-server"] = "vault"
    vttablet_extra_flags[
        "db-credentials-vault-addr"
    ] = f"https://vault-dre.{region}.yelpcorp.com:8200"
    vttablet_extra_flags[
        "db-credentials-vault-path"
    ] = "secrets/vitess/vt-tablet/vttablet_credentials.json"
    vttablet_extra_flags[
        "db-credentials-vault-tls-ca"
    ] = f"/etc/vault/all_cas/acm-privateca-{region}.crt"
    vttablet_extra_flags["db-credentials-vault-ttl"] = "60s"

    if tablet_type == "primary":
        type = "externalmaster"
    else:
        type = "externalreplica"

    config = {
        "cell": cell,
        "name": f"{db_name}_{tablet_type}",
        "type": type,
        "affinity": build_affinity_spec(paasta_pool),
        "extraLabels": build_extra_labels(paasta_pool, paasta_cluster),
        "extraEnv": build_extra_env(paasta_cluster),
        "extraVolumeMounts": [
            {
                "mountPath": "/etc/vault/all_cas",
                "name": "vault-secrets",
                "readOnly": True,
            },
            {
                "mountPath": "/etc/vitess_keyspace_acls",
                "name": "acls",
                "readOnly": True,
            },
            {
                "mountPath": "etc/credentials.yaml",
                "name": "vttablet-fake-credentials",
                "readOnly": True,
            },
            {
                "mountPath": "/etc/init_db.sql",
                "name": "keyspace-fake-init-script",
                "readOnly": True,
            },
        ],
        "extraVolumes": [
            {"name": "vault-secrets", "hostPath": {"path": "/nail/etc/vault/all_cas"}},
            {
                "name": "acls",
                "hostPath": {"path": "/nail/srv/configs/vitess_keyspace_acls"},
            },
            {"name": "vttablet-fake-credentials", "hostPath": {"path": "/dev/null"}},
            {"name": "keyspace-fake-init-script", "hostPath": {"path": "/dev/null"}},
        ],
        "replicas": 1,
        "vttablet": {
            "extraFlags": vttablet_extra_flags,
            "resources": {
                "requests": {
                    "cpu": "100m",
                    "memory": "256Mi",
                },
                "limits": {"memory": "256Mi"},
            },
        },
        "externalDatastore": {
            "database": db_name,
            "host": SOURCE_DB_HOST,
            "port": port,
            "user": "vt_app",
            "credentialsSecret": {
                "key": "/etc/credentials.yaml",
                "volumeName": "vttablet-fake-credentials",
            },
        },
        "dataVolumeClaimTemplate": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "10Gi"}},
            "storageClassName": "ebs-csi-gp3",
        },
    }

    vttablet_extra_env = copy.deepcopy(VTTABLET_EXTRA_ENV)
    for env in vttablet_extra_env:
        if env["name"] == "TOPOLOGY_FLAGS":
            env[
                "value"
            ] = f"--topo_implementation {TOPO_IMPLEMENTATION} --topo_global_server_address ${zk_address} --topo_global_root {TOPO_GLOBAL_ROOT}"
        if env["name"] == "CELL_TOPOLOGY_SERVERS":
            env["value"] = zk_address
        if env["name"] == "DB":
            env["value"] = db_name
        if env["name"] == "KEYSPACE":
            env["value"] = keyspace
        if env["name"] == "VAULT_ADDR":
            env["value"] = f"https://vault-dre.{region}.yelpcorp.com:8200"
        if env["name"] == "VAULT_CACERT":
            env["value"] = f"/etc/vault/all_cas/acm-privateca-{region}.crt"
        config["extraEnv"].append(env)

    # Add extra pod label to filter
    config["extraLabels"]["tablet_type"] = f"{db_name}_{tablet_type}"

    return config


def build_keyspaces_config(
    cells, keyspaces, paasta_pool, paasta_cluster, zk_address, region
):
    """
    Build vitess keyspace config
    """
    config = []

    for keyspace_config in keyspaces:
        keyspace = keyspace_config["keyspace"]
        db_name = keyspace_config["keyspace"]
        cluster = keyspace_config["cluster"]

        tablet_pools = []

        mysql_port_mappings = load_system_paasta_config().get_mysql_port_mappings()

        # Build vttablets
        for tablet_type in TABLET_TYPES:
            # We don't have migration or reporting tablets in all clusters
            if tablet_type not in mysql_port_mappings[cluster]:
                continue
            port = mysql_port_mappings[cluster][tablet_type]

            # We use migration_replication delay for migration tablets and read_replication_delay for everything else
            # Also throttling threshold for migration tablets is 2 hours, refresh and sanitized primaries at 30 seconds and everything else at 3 seconds
            if tablet_type == "migration":
                throttle_query_table = "migration_replication_delay"
                throttle_metrics_threshold = "7200"
            else:
                throttle_query_table = "read_replication_delay"
                if cluster.startswith("refresh") or cluster.startswith("sanitized"):
                    throttle_metrics_threshold = "30"
                else:
                    throttle_metrics_threshold = "3"

            tablet_pools.extend(
                [
                    build_tablet_pool_config(
                        cell,
                        db_name,
                        keyspace,
                        port,
                        paasta_pool,
                        paasta_cluster,
                        zk_address,
                        throttle_query_table,
                        throttle_metrics_threshold,
                        tablet_type,
                        region,
                    )
                    for cell in cells
                ]
            )

        config.append(
            {
                "name": keyspace,
                "durabilityPolicy": "none",
                "turndownPolicy": "Immediate",
                "partitionings": [
                    {
                        "equal": {
                            "parts": 1,
                            "shardTemplate": {
                                "databaseInitScriptSecret": {
                                    "volumeName": "keyspace-fake-init-script",
                                    "key": "/etc/init_db.sql",
                                },
                                "tabletPools": tablet_pools,
                            },
                        }
                    }
                ],
            }
        )

    return config


log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())


class VitessDeploymentConfigDict(LongRunningServiceConfigDict, total=False):
    replicas: int


class VitessDeploymentConfig(LongRunningServiceConfig):
    config_dict: VitessDeploymentConfigDict

    config_filename_prefix = "vitesscluster"

    def __init__(
        self,
        service: str,
        cluster: str,
        instance: str,
        config_dict: VitessDeploymentConfigDict,
        branch_dict: Optional[BranchDictV2],
        soa_dir: str = DEFAULT_SOA_DIR,
    ) -> None:

        super().__init__(
            cluster=cluster,  # superregion
            instance=instance,  # host-1
            service=service,  # vitess
            soa_dir=soa_dir,
            config_dict=config_dict,
            branch_dict=branch_dict,
        )

    def get_instances(self, with_limit: bool = True) -> int:
        return self.config_dict.get("replicas", 1)

    def validate(
        self,
        params: List[str] = [
            "cpus",
            "security",
            "dependencies_reference",
            "deploy_group",
        ],
    ) -> List[str]:
        # Use InstanceConfig to validate shared config keys like cpus and mem
        # TODO: add mem back to this list once we fix PAASTA-15582 and
        # move to using the same units as flink/marathon etc.
        error_msgs = super().validate(params=params)

        if error_msgs:
            name = self.get_instance()
            return [f"{name}: {msg}" for msg in error_msgs]
        else:
            return []


def generate_vitess_instance_config(
    instance_config: Dict,
) -> Dict:
    # Generate the vitess instance config from the yelpsoa config

    cpus = instance_config.get("cpus")
    mem = instance_config.get("mem")
    deploy_group = instance_config.get("deploy_group")
    zk_address = instance_config.get("zk_address")
    paasta_pool = instance_config.get("paasta_pool")
    paasta_cluster = instance_config.get("paasta_cluster")
    cells = instance_config.get("cells")
    keyspaces = instance_config.get("keyspaces")
    region = instance_config.get("region")

    vitess_instance_config = {
        "namespace": "paasta-vitessclusters",
        "cpus": cpus,
        "mem": mem,
        "min_instances": 1,
        "max_instances": 1,
        "deploy_group": deploy_group,
        "autoscaling": {"setpoint": 0.7},
        "env": {
            "OPERATOR_NAME": "vitess-operator",
            "POD_NAME": "vitess-k8s",
            "PS_OPERATOR_POD_NAME": "vitess-k8s",
            "PS_OPERATOR_POD_NAMESPACE": "paasta-vitessclusters",
            "WATCH_NAMESPACE": "paasta-vitessclusters",
        },
        "healthcheck_grace_period_seconds": 60,
        "healthcheck_mode": "cmd",
        "healthcheck_cmd": "true",
        "images": {
            "vtctld": VTCTLD_IMAGE,
            "vtadmin": VT_ADMIN_IMAGE,
            "vtgate": VT_GATE_IMAGE,
            "vttablet": VT_TABLET_IMAGE,
        },
        "globalLockserver": {
            "external": {
                "implementation": TOPO_IMPLEMENTATION,
                "address": zk_address,
                "rootPath": TOPO_GLOBAL_ROOT,
            }
        },
        "cells": [
            build_cell_config(cell, paasta_pool, paasta_cluster, region)
            for cell in cells
        ],
        "vitessDashboard": build_vitess_dashboard_config(
            cells, paasta_pool, paasta_cluster, zk_address
        ),
        "vtadmin": build_vt_admin_config(cells, paasta_pool, paasta_cluster),
        "keyspaces": build_keyspaces_config(
            cells, keyspaces, paasta_pool, paasta_cluster, zk_address, region
        ),
        "updateStrategy": {"type": "Immediate"},
    }
    return vitess_instance_config


def load_vitess_service_instance_configs(
    service: str,
    instance: str,
    instance_type: str,
    cluster: str,
    soa_dir: str = DEFAULT_SOA_DIR,
) -> VitessDeploymentConfigDict:
    general_config = service_configuration_lib.read_service_configuration(
        service, soa_dir=soa_dir
    )
    instance_config = load_service_instance_config(
        service, instance, instance_type, cluster, soa_dir=soa_dir
    )
    vitess_instance_config = generate_vitess_instance_config(instance_config)

    general_config = deep_merge_dictionaries(
        overrides=vitess_instance_config, defaults=general_config
    )
    return general_config


def load_vitess_instance_config(
    service: str,
    instance: str,
    cluster: str,
    load_deployments: bool = True,
    soa_dir: str = DEFAULT_SOA_DIR,
) -> VitessDeploymentConfig:
    general_config = load_vitess_service_instance_configs(
        service, instance, "vitesscluster", cluster, soa_dir=soa_dir
    )

    branch_dict: Optional[BranchDictV2] = None
    if load_deployments:
        deployments_json = load_v2_deployments_json(service, soa_dir=soa_dir)
        temp_instance_config = VitessDeploymentConfig(
            service=service,
            cluster=cluster,
            instance=instance,
            config_dict=general_config,
            branch_dict=None,
            soa_dir=soa_dir,
        )
        branch = temp_instance_config.get_branch()
        deploy_group = temp_instance_config.get_deploy_group()
        branch_dict = deployments_json.get_branch_dict(service, branch, deploy_group)

    return VitessDeploymentConfig(
        service=service,
        cluster=cluster,
        instance=instance,
        config_dict=general_config,
        branch_dict=branch_dict,
        soa_dir=soa_dir,
    )


# TODO: read this from CRD in service configs
def cr_id(service: str, instance: str) -> Mapping[str, str]:
    return dict(
        group="yelp.com",
        version="v1alpha1",
        namespace=KUBERNETES_NAMESPACE,
        plural="vitessclusters",
        name=sanitised_cr_name(service, instance),
    )
