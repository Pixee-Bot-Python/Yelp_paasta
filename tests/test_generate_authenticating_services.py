# Copyright 2015-2016 Yelp Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from unittest.mock import patch

import pytest
import yaml

from paasta_tools import generate_authenticating_services


@pytest.fixture
def mock_soa_configs(tmpdir):
    soa_dir = tmpdir / "soa_configs"
    soa_dir.mkdir()
    service_a = soa_dir / "service_a"
    service_a.mkdir()
    with (service_a / "authorization.yaml").open("w") as f:
        yaml.safe_dump(
            {
                "authorization": {
                    "rules": [
                        {"identity_groups": {"services": ["service_b", "service_c"]}},
                        {"identity_groups": {"services": ["service_d"]}},
                    ],
                },
            },
            f,
        ),
    service_b = soa_dir / "service_b"
    service_b.mkdir()
    with (service_b / "authorization.yaml").open("w") as f:
        yaml.safe_dump(
            {
                "authorization": {
                    "rules": [
                        {"identity_groups": {"services": ["service_a", "service_d"]}},
                    ],
                },
            },
            f,
        ),
    with patch(
        "paasta_tools.generate_authenticating_services.DEFAULT_SOA_DIR", str(soa_dir)
    ):
        yield


def test_enumerate_authenticating_services(mock_soa_configs):
    assert generate_authenticating_services.enumerate_authenticating_services() == {
        "services": ["service_a", "service_b", "service_c", "service_d"],
    }


@patch("paasta_tools.utils.socket")
@patch("paasta_tools.utils.datetime")
@patch("paasta_tools.generate_authenticating_services.parse_args")
def test_main_yaml_config(
    mock_parse_args, mock_datetime, mock_socket, tmpdir, mock_soa_configs
):
    output = tmpdir / "authenticating.yaml"
    mock_datetime.datetime.now().isoformat.return_value = "$SOME_TIME"
    mock_socket.getfqdn.return_value = "somehost.yelp"
    mock_parse_args.return_value.output_filename = str(output)
    mock_parse_args.return_value.output_format = "yaml"

    generate_authenticating_services.main()

    with output.open() as f:
        assert (
            f.read()
            == """
# This file is automatically generated by paasta_tools.
# It was automatically generated at $SOME_TIME on somehost.yelp.
---
services:
- service_a
- service_b
- service_c
- service_d
""".lstrip()
        )
