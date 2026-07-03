# CLAUDE.MD - EKS Cluster Upgrade Project Guide

This document provides comprehensive context for AI assistants working with the eks-cluster-upgrade project.

## Project Overview

**eksupgrade** is a production-ready Python CLI tool that automates Amazon EKS (Elastic Kubernetes Service) cluster upgrades. It orchestrates the complex process of upgrading control planes, managed node groups, self-managed node groups, and cluster add-ons with intelligent error handling and recovery.

- **Version**: 0.10.0
- **License**: MIT-0
- **Python**: 3.10+
- **Package**: Available on PyPI as `eksupgrade`

## Core Purpose

The tool addresses the challenging task of coordinating multiple upgrade operations:
1. EKS control plane version upgrade (AWS-managed)
2. Managed node group upgrades (EKS-managed ASGs)
3. Self-managed node group upgrades (custom ASG-based)
4. Cluster add-on upgrades (vpc-cni, kube-proxy, coredns, etc.)
5. Kubernetes operational concerns (pod disruption budgets, node draining, autoscaler management)

## Project Structure

```
eksupgrade/
├── __init__.py                 # Package version (0.10.0)
├── __main__.py                 # CLI entry point
├── cli.py                      # Main CLI command handler (Typer-based)
├── exceptions.py               # Custom exception classes
├── utils.py                    # Utility functions (logging, version parsing)
├── starter.py                  # Upgrade orchestration and threading logic
├── models/                     # Data models
│   ├── base.py                 # Base resource classes (BaseResource, AwsResource)
│   └── eks.py                  # EKS resource models (Cluster, ManagedNodeGroup, etc.)
└── src/                        # Core functionality
    ├── boto_aws.py             # AWS boto3 operations (EC2, ASG, EKS)
    ├── k8s_client.py           # Kubernetes client operations (drain, evict, autoscaler)
    ├── karpenter.py            # Karpenter drift-based node upgrade logic
    ├── preflight.py            # Read-only pre-upgrade checks (--preflight)
    ├── rollback.py             # Rollback readiness insights + addon compat
    ├── eks_get_image_type.py   # AMI/image type detection
    ├── latest_ami.py           # Latest AMI retrieval from AWS SSM/EC2
    └── self_managed.py         # Self-managed node group upgrade logic
```

Addon versions are resolved live via `describe_addon_versions` — there are no
embedded version-mapping files (the old `src/S3Files/` directory was removed).

## Key Technologies

- **CLI Framework**: Typer with rich output
- **AWS SDK**: boto3 (v1.34+) with type stubs
- **Kubernetes Client**: kubernetes library (v29.0.0-v34.x)
- **Version Management**: packaging library for semantic versioning
- **Testing**: pytest with moto for AWS mocking
- **Code Quality**: black, isort, ruff, mypy, pydocstyle, bandit, safety
- **Package Management**: Poetry

## Architecture and Design Patterns

### Model Hierarchy

The project uses an object-oriented design with a clear inheritance hierarchy:

```
BaseResource (abstract base)
├── AwsResource (adds AWS session management)
│   ├── AwsRegionResource (adds region-specific operations)
│   │   ├── EksResource (base for all EKS resources)
│   │   │   ├── Cluster (central cluster representation)
│   │   │   ├── ManagedNodeGroup (EKS-managed nodes)
│   │   │   ├── ClusterAddon (vpc-cni, kube-proxy, coredns)
│   │   │   └── AutoscalingGroup (self-managed nodes)
```

**Key Design Principles**:
- **Lazy Loading**: Resources use `@cached_property` for expensive AWS API calls
- **Method Decorators**: `@requires_cluster` validates cluster state before operations
- **Separation of Concerns**: Models (data) vs. src/ (operations) vs. cli.py (orchestration)
- **Type Safety**: Extensive use of type hints and mypy validation

### Key Components

#### 1. models/eks.py (1002 lines)

**Cluster Class** - Central orchestrator:
- Properties: name, version, status, endpoint, roleArn, platformVersion
- Cached properties: addons, nodegroups, autoscaling_groups, available_addon_versions
- Methods:
  - `get()` - Fetch cluster details from AWS
  - `update_cluster(version)` - Request control plane upgrade
  - `upgrade_addons()` - Upgrade all EKS add-ons with compatibility logic
  - `upgrade_nodegroups()` - Upgrade all managed node groups
  - `wait_for_active()` - Poll until cluster is ACTIVE
- Version tracking with semantic versioning

**ManagedNodeGroup Class**:
- Properties: name, cluster, version, amiType, capacityType, needs_upgrade
- Methods: get(), update(), wait_for_active()

**ClusterAddon Class**:
- Handles: vpc-cni (with step-wise upgrade logic), kube-proxy, coredns
- Version compatibility checks against cluster version
- Support for default vs. latest versions

**AutoscalingGroup Class**:
- Tracks self-managed node groups via tags
- Properties: launch configuration, instances, capacity, health status

#### 2. cli.py - Command Handler

**Main Function Signature**:
```python
def main(
    cluster_name: str,
    cluster_version: str,
    region: str,
    max_retry: int = 2,
    force: bool = False,
    preflight: bool = False,  # run read-only checks and exit (no upgrade)
    parallel: bool = False,
    latest_addons: bool = False,
    disable_checks: bool = False,
    interactive: bool = True,
)
```

**Workflow**:
1. Initialize AWS session and Cluster object
2. If `--preflight`: run read-only checks (src/preflight.py) and exit with 0/1/2 — no upgrade
3. Validate cluster state (ACTIVE, not UPDATING)
4. Interactive confirmation prompt (if enabled)
5. Upgrade control plane
5. Wait for control plane upgrade completion
6. Upgrade add-ons (vpc-cni, kube-proxy, coredns)
7. Upgrade managed node groups
8. Upgrade self-managed node groups (via starter.py)
9. Handle autoscaler pause/resume

#### 3. starter.py - Upgrade Orchestration

**Self-Managed Node Group Upgrade Process**:
1. Pause Cluster Autoscaler (Karpenter is NOT paused — its nodes upgrade via drift, see src/karpenter.py)
2. For each Auto Scaling Group:
   - Detect current AMI type (AL2, Windows, Bottlerocket, Ubuntu)
   - Fetch latest AMI for target Kubernetes version
   - Create new launch configuration
   - Update ASG with new launch configuration
   - Drain and cordon old nodes (handle PDBs)
   - Terminate old instances
   - Monitor new node readiness
3. Resume the Cluster Autoscaler (in cli.py's finally — runs even on failure/Ctrl+C)

**Threading Support**: Optional parallel upgrades using threading.Thread

#### 4. src/k8s_client.py - Kubernetes Operations

**Key Functions**:
- `get_bearer_token(cluster_id, region)` - Generate EKS auth token via AWS STS
- `loading_config(cluster_name, region)` - Configure the default kubernetes client (CA + bearer token)
- `drain_nodes(cluster_name, node_name, forced, region)` - Drain all pods via the eviction API (PDB 429s retried; DaemonSet/mirror pods skipped)
- `unschedule_old_nodes()` / `delete_node()` / `find_node()` - Node cordon/delete/lookup
- `is_cluster_auto_scaler_present()` / `cluster_auto_enable_disable()` - Detect and pause/resume the Cluster Autoscaler
- `is_karpenter_present()` - Detect Karpenter (the controller is never paused; drift handles its nodes)
- `get_statefulset_pods_on_node()` / `wait_for_statefulset_pods_ready()` - StatefulSet PVC-rebind confirmation

**Authentication**: Uses AWS STS GetCallerIdentity to generate bearer token for EKS

#### 5. src/boto_aws.py - AWS Operations

**Key Functions**:
- `wait_for_asg_instances_healthy()` - Monitor ASG instance health
- `get_lt_lt_user_data()` - Extract launch template user data
- `create_launch_configuration()` - Create new LC with updated AMI
- `terminate_ec2_instances()` - Terminate old instances

#### 6. src/latest_ami.py - AMI Management

**Supports Multiple AMI Types**:
- Amazon Linux 2 (AL2_x86_64, AL2_x86_64_GPU, AL2_ARM_64)
- Windows Server (WINDOWS_CORE_*, WINDOWS_FULL_*)
- Bottlerocket (BOTTLEROCKET_*)
- Ubuntu (custom queries)

**AMI Lookup**: Uses AWS SSM Parameter Store and EC2 DescribeImages APIs

#### 7. src/self_managed.py - Self-Managed Node Logic

**Core Function**: `upgrade_self_managed_nodes()`
- Orchestrates entire self-managed upgrade workflow
- Handles autoscaler management
- Implements retry logic
- Error recovery with autoscaler restoration

## Upgrade Workflow

### Step-by-Step Process

1. **Pre-Upgrade Validation**
   - Verify cluster exists and is ACTIVE
   - Check cluster is not already in UPDATING state
   - Validate target version is valid upgrade path (no multi-minor jumps)

2. **Control Plane Upgrade**
   - Call `cluster.update_cluster(target_version)`
   - Poll `cluster.wait_for_active()` until upgrade completes
   - Control plane is managed entirely by AWS

3. **Add-on Upgrades**
   - **vpc-cni**: Step-wise upgrade by minor version (e.g., 1.10 -> 1.11 -> 1.12)
   - **kube-proxy**: Direct upgrade to compatible version
   - **coredns**: Direct upgrade to compatible version
   - Versions resolved live via `describe_addon_versions` (no embedded mappings)

4. **Managed Node Group Upgrades**
   - AWS handles AMI updates and rolling upgrades
   - Tool triggers upgrade and monitors completion
   - Parallel execution optional

5. **Self-Managed Node Group Upgrades**
   - **Pause**: Cluster Autoscaler and Karpenter
   - **For each ASG**:
     - Detect current AMI type from launch configuration
     - Query latest AMI for target Kubernetes version
     - Create new launch configuration with updated AMI
     - Update ASG to use new launch configuration
     - **For each old node**:
       - Cordon node (prevent new pod scheduling)
       - Drain node (evict pods with timeout)
       - Handle Pod Disruption Budgets (respect or force)
       - Terminate EC2 instance
       - Wait for replacement node to be ready
   - **Resume**: Cluster Autoscaler and Karpenter (always, even on failure)

### Version Compatibility Logic

**vpc-cni Step Upgrade Example**:
- Current: vpc-cni v1.10.x on cluster v1.24
- Target: Cluster v1.26
- Process:
  1. Upgrade vpc-cni to v1.11.x (compatible with 1.24-1.25)
  2. Upgrade vpc-cni to v1.12.x (compatible with 1.25-1.26)
  3. Then upgrade cluster control plane

**Version Parsing**: Uses `packaging.version.parse()` for semantic version comparison

## Error Handling

### Custom Exceptions (exceptions.py)

- `BaseEksUpgradeException` - Base exception for the package
- `EksException` - General EKS-level error
- `ClusterException` / `ClusterInactiveException` - Cluster state errors
- `EksUpgradeNotImplementedError` - Unimplemented operation
- `InvalidUpgradeTargetVersion` - Multi-minor jump or invalid rollback target

### Recovery Mechanisms

1. **Autoscaler Recovery**: Always resume autoscalers in `finally` block
2. **Retry Logic**: Configurable `max_retry` for transient failures
3. **Force Flag**: Override PDB constraints when necessary
4. **State Validation**: Check resource state before operations

## Testing Strategy

### Test Infrastructure

**Framework**: pytest with moto for AWS service mocking

**Key Fixtures** (tests/conftest.py):
- `aws_creds` - Mock AWS credentials
- `eks_client`, `ec2_client`, `sts_client`, `autoscaling_client` - Mocked AWS clients
- `eks_cluster` - Pre-created mock EKS cluster
- `app` - Typer CLI application instance

### Test Suites

1. **test_models_eks.py** - Cluster, add-on, node group model tests
2. **test_models_base.py** - Base resource functionality
3. **test_utils.py** - Utility function tests
4. **test_k8s_client.py** - Kubernetes client operations
5. **test_eks_get_image_type.py** - AMI type detection
6. **test_cli.py** - CLI command and argument parsing

### CI/CD Validation (.github/workflows/)

**validate.yaml** (the only workflow):
- Checks: `poetry poe check` (formatting/lint)
- Unit tests: pytest on Python 3.10
- Security: `poetry poe safety`

There is no e2e or publish workflow in this repository.

## Development Guidelines

### Code Style

- **Line Length**: 120 characters (black, isort)
- **Import Sorting**: isort with black compatibility
- **Type Hints**: Required for all public functions
- **Docstrings**: Google-style docstrings (pydocstyle)
- **Linting**: ruff for fast Python linting

### Running Tests

```bash
# Install dependencies
poetry install

# Run all tests
poetry run pytest

# Run with coverage
poetry run pytest --cov=eksupgrade --cov-report=html

# Run specific test file
poetry run pytest tests/test_cli.py
```

### Code Quality Checks

```bash
# Format code
poetry run black eksupgrade/
poetry run isort eksupgrade/

# Lint
poetry run ruff eksupgrade/
poetry run pydocstyle eksupgrade/

# Type check
poetry run mypy eksupgrade/

# Security scan
poetry run bandit -r eksupgrade/
poetry run safety check
```

### Using Poetry Tasks (pyproject.toml)

```bash
poetry run poe test      # Run tests
poetry run poe lint      # Run all linters
poetry run poe fix       # Auto-fix formatting
poetry run poe security  # Run security checks
poetry run poe build     # Build package
```

### Pre-commit Hooks

The project uses pre-commit hooks configured in `.pre-commit-config.yaml`. Install with:

```bash
poetry run pre-commit install
```

## AWS Permissions Required

### Minimum IAM Policy

The tool requires the following AWS permissions:

**IAM**:
- `iam:GetRole`
- `sts:GetAccessKeyInfo`
- `sts:GetCallerIdentity`
- `sts:GetSessionToken`

**EC2**:
- `ec2:Describe*`
- `autoscaling:Describe*`
- `autoscaling:UpdateAutoScalingGroup`
- `autoscaling:CreateLaunchConfiguration`
- `autoscaling:DeleteLaunchConfiguration`
- `ec2:TerminateInstances`
- `ssm:GetParameter`

**EKS**:
- `eks:DescribeCluster`
- `eks:DescribeNodegroup`
- `eks:DescribeAddon`
- `eks:ListAddons`
- `eks:ListNodegroups`
- `eks:UpdateClusterVersion`
- `eks:UpdateNodegroupVersion`
- `eks:UpdateAddon`

## Common Patterns and Idioms

### Accessing AWS Resources

```python
from eksupgrade.models.eks import Cluster

# Initialize cluster
cluster = Cluster(name="my-cluster", region="us-east-1")

# Lazy load cluster details (cached)
print(cluster.version)  # Triggers API call first time
print(cluster.version)  # Uses cached value

# Explicitly refresh
cluster.get()  # Force API call
```

### Version Comparison

```python
from packaging.version import parse as parse_version

current_version = parse_version("1.24")
target_version = parse_version("1.26")

if target_version > current_version:
    print("Upgrade needed")
```

### Error Handling

```python
from eksupgrade.exceptions import InvalidUpgradeTargetVersion

try:
    cluster.update_cluster(wait=True)
except InvalidUpgradeTargetVersion:
    logger.error("Multi-minor jump requested; run once per minor")
    raise
```

### Kubernetes Operations

```python
from eksupgrade.src.k8s_client import k8s_client_dict, drain_node

# Create kubernetes client
k8s_clients = k8s_client_dict(cluster)

# Drain node
drain_node(
    k8s_client=k8s_clients,
    node_name="ip-10-0-1-234.ec2.internal",
    force=False  # Respect PDBs
)
```

## Important Considerations

### Version Upgrade Constraints

- **No Multi-Minor Jumps**: EKS only supports sequential minor version upgrades
  - Valid: 1.24 -> 1.25 -> 1.26
  - Invalid: 1.24 -> 1.26
- **Add-on Compatibility**: Each add-on has specific version requirements per cluster version
- **vpc-cni Special Handling**: Requires step-wise minor version upgrades

### Self-Managed Node Group Considerations

- **AMI Type Detection**: Tool auto-detects AL2, Windows, Bottlerocket, Ubuntu
- **User Data Preservation**: Launch configuration user data is copied to new LC
- **Node Draining**: Respects Pod Disruption Budgets by default (use `--force` to override)
- **Autoscaler Management**: Automatically pauses/resumes to prevent interference

### Parallel Execution

- **Managed Node Groups**: Can be upgraded in parallel with `--parallel` flag
- **Self-Managed Node Groups**: Uses threading for concurrent ASG upgrades
- **Trade-offs**: Faster completion vs. increased cluster load

### Interactive Mode

- **Default**: Prompts user for confirmation before starting upgrade
- **Non-interactive**: Use `--no-interactive` for automated CI/CD pipelines

## Troubleshooting

### Common Issues

1. **Cluster Already Updating**
   - Error: Cluster is in UPDATING state
   - Solution: Wait for current operation to complete

2. **Pod Disruption Budget Violations**
   - Error: Cannot drain node due to PDB
   - Solution: Use `--force` flag to override PDB constraints

3. **AMI Type Detection Failure**
   - Error: Cannot determine AMI type from launch configuration
   - Solution: Check launch configuration user data format

4. **Autoscaler Not Resuming**
   - Error: Cluster Autoscaler still paused after upgrade
   - Solution: Manually scale deployment (tool includes finally block for this)

### Logging

The tool uses Python's logging module with format:
```
%(asctime)s - %(levelname)s - %(name)s:%(lineno)d - %(message)s
```

Set log level via environment:
```bash
export LOG_LEVEL=DEBUG
eksupgrade upgrade my-cluster 1.26 us-east-1
```

## Entry Points

### CLI Usage

```bash
# Basic upgrade
eksupgrade upgrade my-cluster 1.26 us-east-1

# With options
eksupgrade upgrade my-cluster 1.26 us-east-1 \
  --parallel \
  --latest-addons \
  --force \
  --max-retry 3

# Non-interactive (CI/CD)
eksupgrade upgrade my-cluster 1.26 us-east-1 --no-interactive

# Show version
eksupgrade --version

# Show help
eksupgrade --help
```

### Python API Usage

```python
from eksupgrade.models.eks import Cluster
from eksupgrade.starter import upgrade_self_managed_nodes

# Initialize cluster
cluster = Cluster(name="my-cluster", region="us-east-1")

# Upgrade control plane
cluster.update_cluster("1.26")
cluster.wait_for_active()

# Upgrade add-ons
cluster.upgrade_addons(latest=False)

# Upgrade managed node groups
cluster.upgrade_nodegroups(parallel=False)

# Upgrade self-managed nodes
upgrade_self_managed_nodes(
    cluster=cluster,
    max_retry=2,
    force=False,
    parallel=False
)
```

## Package Distribution

### Installation

```bash
# From PyPI
pip install eksupgrade

# From source
git clone https://github.com/aws-samples/eks-cluster-upgrade.git
cd eks-cluster-upgrade
poetry install
```

### Building and Publishing

```bash
# Build package
poetry build

# Publish to PyPI (manual)
poetry publish
```

There is no automated publish workflow in this repository.

## Related Documentation

- **README.md**: User-facing documentation with usage examples
- **CONTRIBUTING.md**: Contribution guidelines and code of conduct
- **CODE_OF_CONDUCT.md**: Amazon Open Source Code of Conduct
- **pyproject.toml**: Complete project configuration and dependencies
- **.github/workflows/**: CI/CD pipeline definitions

## Key Files for Different Tasks

### When Modifying Upgrade Logic
- **starter.py**: Self-managed node upgrade orchestration
- **models/eks.py**: Cluster, add-on, node group models
- **src/self_managed.py**: Self-managed node group operations

### When Modifying AWS Operations
- **src/boto_aws.py**: EC2, ASG, EKS operations
- **src/latest_ami.py**: AMI lookup logic

### When Modifying Kubernetes Operations
- **src/k8s_client.py**: Node draining, autoscaler management

### When Modifying CLI Interface
- **cli.py**: Command parsing and argument handling
- **__main__.py**: Entry point

### When Modifying Version Logic
- **utils.py**: Version parsing utilities
- **models/eks.py**: `_default_next_minor` / `_previous_minor` and addon version resolution

### When Adding Tests
- **tests/conftest.py**: Shared fixtures
- **tests/test_*.py**: Test suites

## Project Conventions

### Naming Conventions
- **Classes**: PascalCase (e.g., `Cluster`, `ManagedNodeGroup`)
- **Functions**: snake_case (e.g., `upgrade_cluster`, `wait_for_active`)
- **Constants**: UPPER_SNAKE_CASE (e.g., `MAX_RETRY_ATTEMPTS`)
- **Private methods**: Leading underscore (e.g., `_internal_method`)

### File Organization
- **models/**: Data models and resource representations
- **src/**: Operational logic and external service interactions
- **tests/**: Test suites mirroring main package structure
- **Root**: CLI entry points and package metadata

### Git Workflow
- **Branches**: feature/*, bugfix/*, release/*
- **Commits**: Conventional commits format
- **PRs**: Require validation workflow to pass

## External Dependencies to Be Aware Of

### AWS Service Limits
- EKS control plane upgrades: 1 at a time per cluster
- Managed node group updates: Sequential or parallel depending on configuration
- API rate limits: boto3 automatic retry with exponential backoff

### Kubernetes Version Support
- Only sequential single-minor upgrades are allowed (e.g. 1.34 -> 1.35); a
  multi-minor target raises InvalidUpgradeTargetVersion with guidance to run
  once per minor.
- AWS EKS supported versions change quarterly (standard support ~1.33-1.36 as of
  mid-2026). Add-on versions are resolved live via describe_addon_versions, so
  there is no static version table to maintain (the old S3Files/version_dict.json
  was removed).

### Add-on Version Resolution
- Resolved live per target version via `describe_addon_versions` (no static files)
- vpc-cni still steps one minor at a time (graduated upgrade)

## Future Enhancement Considerations

When working on new features, consider:

1. **Fargate Support**: Currently focused on EC2-based node groups
2. **Custom Add-ons**: Support for third-party add-ons beyond AWS-managed
3. **Multi-Cluster Upgrades**: Batch upgrade multiple clusters
4. **Rollback Support**: Automated rollback on failure
5. **Pre-upgrade Checks**: Enhanced validation (deprecated APIs, resource capacity)
6. **Post-upgrade Validation**: Health checks and smoke tests

## Summary

This project is a sophisticated, production-ready tool that automates one of the most complex operational tasks in EKS management. The codebase demonstrates:

- **Strong Type Safety**: Extensive type hints and mypy validation
- **Clean Architecture**: Clear separation between models, operations, and CLI
- **Comprehensive Testing**: Unit tests with AWS mocking and E2E tests
- **Production Quality**: Error handling, logging, retry logic, autoscaler recovery
- **User Experience**: Interactive prompts, progress indication, clear error messages

When working with this codebase, always consider the operational impact on production EKS clusters and prioritize reliability and safety over speed.
