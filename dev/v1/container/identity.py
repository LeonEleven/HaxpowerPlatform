"""Allow only the fixed development project and run-scoped CI projects."""

import re


def require(condition, message):
    if not condition:
        raise ValueError(message)


def identity(project):
    if project == "haxpower-v1-dev":
        return f"{project}.localhost", "hpv1_dev"
    match = re.fullmatch(r"haxpower-v1-ci-([1-9][0-9]{0,19})-([1-9][0-9]{0,5})", project)
    require(match is not None, "unknown Compose project")
    run, attempt = match.groups()
    return f"{project}.localhost", f"hpv1_ci_{run}_{attempt}"
