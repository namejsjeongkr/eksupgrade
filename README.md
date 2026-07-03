# Amazon EKS Upgrade Utility (Karpenter-aware fork)

🌐 **Language**: **English** · [한국어](README.ko.md)

`eksupgrade` is a CLI utility that automates the upgrade process for Amazon EKS
clusters — the control plane, managed add-ons, and worker nodes managed by
**Cluster Autoscaler, self-managed ASGs, and Karpenter**.

> **About this fork.** This repository began as a fork of
> [`aws-samples/eks-cluster-upgrade`](https://github.com/aws-samples/eks-cluster-upgrade),
> which is no longer actively maintained. It is now an independent project. The
> headline change is **Karpenter-native, drift-based node upgrades** (instead of
> terminating instances), alongside restored Cluster Autoscaler support and
> safer node draining. See [Differences from upstream](#differences-from-upstream).

## Differences from upstream

- **Karpenter nodes upgrade via Drift, not termination.** The Karpenter
  controller is left **running**; after the control plane is upgraded,
  alias-based `EC2NodeClass` selectors re-resolve to the AMI for the new
  Kubernetes version and Karpenter replaces nodes **capacity-first**, honoring
  PodDisruptionBudgets, disruption budgets, and `karpenter.sh/do-not-disrupt`.
  The old approach (pause controller → terminate EC2) caused a capacity gap,
  bypassed PDBs, and never actually updated the AMI.
- **Both autoscalers supported side by side.** Cluster Autoscaler is detected
  (including Helm-named installs, via label selector) and paused/resumed; the
  Karpenter path is separate and never applies CA semantics.
- **Owner-aware node draining.** Draining skips DaemonSet and static/mirror
  pods, and refuses to start if an unmanaged pod would be lost (unless
  `--force`), so a node is never left half-drained.

## Cluster Upgrade

The upgrade runs in the following order to keep the cluster stable at each step:

```
┌─────────────────────────────────────────────────────────────────┐
│                     EKS Cluster Upgrade Flow                      │
└─────────────────────────────────────────────────────────────────┘

  [1] Control Plane Upgrade
      └─ AWS manages the upgrade; eksupgrade waits until ACTIVE.
         (Karpenter drift begins here for alias-based EC2NodeClasses.)

  [2] Add-on Upgrades  (vpc-cni → kube-proxy → coredns)
      └─ Versions resolved live from the EKS API per cluster version;
         vpc-cni is upgraded step-by-step per minor version.

  [3] Cluster Autoscaler → PAUSE   (Karpenter is left RUNNING)
      └─ CA deployment scaled to 0 so it can't fight node replacement.
         Karpenter must keep running for its drift to replace nodes.

  [4] Managed Node Group Upgrade
      └─ EKS performs a rolling AMI replacement (--parallel optional).

  [5] Self-managed Node Group Upgrade  (ASG-based)
      └─ For each Auto Scaling Group:
           a. Detect AMI type (AL2023 / AL2 / Bottlerocket / Windows / Ubuntu)
           b. Fetch the latest EKS-optimised AMI from SSM
           c. Roll each outdated instance: launch replacement → cordon →
              owner-aware drain (respects PDB unless --force) → terminate

  [6] Karpenter Node Upgrade — via DRIFT  (if Karpenter is detected)
      └─ Inspect each EC2NodeClass:
           • alias selector (e.g. al2023@latest / al2023@vX) → auto-drifts to
             the new Kubernetes version's AMI; the tool only observes + waits
           • id / name / tags selector → will NOT auto-drift; the tool warns
             that those nodes need a manual amiSelectorTerms update
         Then wait (bounded) until the drifting NodePools' nodes are on the
         target version. The controller is never paused or forced.

  [7] Cluster Autoscaler → RESUME
      └─ Restored to its original replica count (also on failure).
         Karpenter needs no resume — it was never paused.
```

### Supported Node Types

| Node Type | Managed Node Group | Self-managed (ASG) | Karpenter |
|-----------|:-----------------:|:------------------:|:---------:|
| Amazon Linux 2023 (AL2023) | ✅ | ✅ | ✅ (drift) |
| Bottlerocket | ✅ | ✅ | ✅ (drift) |
| Windows Server | ✅ | ✅ | ✅ (drift) |
| Ubuntu | ✅ | ✅ | ✅ (drift) |
| Amazon Linux 2 (AL2) | ✅ | ✅ | n/a¹ |

¹ AL2 EKS-optimised AMIs reached end of support; EKS 1.32 was the last version
to ship them. Use AL2023 for 1.33+.

### Supported Kubernetes Versions

Sequential single-minor upgrades only (e.g. `1.34 → 1.35`). EKS does not allow
jumping multiple minors in one run — to go from 1.34 to 1.36, run the tool
twice (`1.34 → 1.35`, then `1.35 → 1.36`).

> EKS standard support currently spans roughly 1.33–1.36 (and a few older
> versions under extended support). The tool resolves add-on versions live from
> the EKS API, so it tracks whatever EKS currently offers rather than a baked-in
> table.

## Pre-Requisites

You need permissions for both AWS and the Kubernetes cluster.

1. Install from source (this fork is not published to PyPI).

   **Recommended — install the `eksupgrade` command with [pipx](https://pipx.pypa.io)**
   (isolated, and puts `eksupgrade` on your `PATH`):

```sh
git clone https://github.com/namejsjeongkr/eksupgrade.git
pipx install ./eksupgrade
eksupgrade --help
```

   To upgrade later after pulling new changes: `pipx install --force ./eksupgrade`.

   **Alternative — for development**, use Poetry inside the cloned repo. Commands
   then run as `poetry run eksupgrade ...`:

```sh
git clone https://github.com/namejsjeongkr/eksupgrade.git
cd eksupgrade
poetry install
poetry run eksupgrade --help
```

2. AWS permissions — an example minimum policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "iam",
      "Effect": "Allow",
      "Action": [
        "iam:GetRole",
        "sts:GetAccessKeyInfo",
        "sts:GetCallerIdentity",
        "sts:GetSessionToken"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ec2",
      "Effect": "Allow",
      "Action": [
        "autoscaling:CreateLaunchConfiguration",
        "autoscaling:Describe*",
        "autoscaling:SetDesiredCapacity",
        "autoscaling:TerminateInstanceInAutoScalingGroup",
        "autoscaling:UpdateAutoScalingGroup",
        "ec2:Describe*",
        "ec2:TerminateInstances",
        "ssm:GetParameter"
      ],
      "Resource": "*"
    },
    {
      "Sid": "eks",
      "Effect": "Allow",
      "Action": [
        "eks:Describe*",
        "eks:List*",
        "eks:UpdateAddon",
        "eks:UpdateClusterVersion",
        "eks:UpdateNodegroupVersion"
      ],
      "Resource": "*"
    }
  ]
}
```

3. Update your local kubeconfig to authenticate to the cluster:

```sh
aws eks update-kubeconfig --name <CLUSTER-NAME> --region <REGION>
```

## Usage

> Examples below use the `eksupgrade` command (pipx install). If you installed
> with Poetry for development, prefix each command with `poetry run`.

```sh
eksupgrade --help
```

```sh
 Usage: eksupgrade [OPTIONS] COMMAND [ARGS]...

 Commands:
   upgrade    Upgrade a target cluster to the next Kubernetes version.
   rollback   Roll the cluster back one minor version (within 7 days of an
              in-place upgrade).

 Options:
   --version                   Show the version and exit
   --help                      Show this message and exit
```

```sh
 Usage: eksupgrade upgrade [OPTIONS] CLUSTER_NAME CLUSTER_VERSION REGION

 Arguments:
   cluster_name      The name of the cluster to be upgraded   [required]
   cluster_version   The target Kubernetes version            [required]
   region            The AWS region of the target cluster     [required]

 Options:
   --max-retry        INTEGER  Retries per upgrade            [default: 2]
   --force                     Force pod eviction (ignores PDB / unmanaged pods)
   --parallel                  Upgrade node groups in parallel
   --latest-addons             Use the latest eligible add-on versions
   --interactive               Prompt for confirmation        [default: on]
   --help                      Show this message and exit
```

Example:

```sh
eksupgrade upgrade my-cluster 1.35 ap-northeast-2
```

> **Breaking change**: earlier releases were invoked without a subcommand
> (`eksupgrade <cluster> <version> <region>`). The upgrade flow now lives
> under `eksupgrade upgrade ...`.

### Read-only preflight

Run a read-only assessment without changing anything:

```sh
eksupgrade upgrade <cluster> <target-version> <region> --preflight --no-interactive
```

It checks the control plane, add-ons, managed node groups, Karpenter, and
PodDisruptionBudget coverage (warning on replicas≥2 workloads with no PDB), prints
a summary report, and exits without performing any upgrade. Exit codes: `0` safe
(warnings allowed), `1` blocking issues found, `2` the checks could not run.

### Version rollback

Roll a cluster back one minor version within 7 days of an in-place upgrade
(EKS version rollback, 2026-07):

```sh
eksupgrade rollback <cluster> <region>
```

The target is always N-1 (computed from the current version). The order is the
reverse of an upgrade, per the version skew policy: managed node groups are
rolled back first, then the control plane; Karpenter alias-based nodes drift
back afterwards. Before anything mutates, the cluster's `ROLLBACK_READINESS`
insights are shown — `ERROR`/`UNKNOWN` findings block unless `--force` (which
bypasses insight checks only; EKS still enforces the 7-day window, the
in-place-upgrade requirement, and sequential rollback server-side).

Not rolled back (reported for manual action): **add-ons** (incompatible
versions are listed — downgrade them first) and **self-managed node groups**.

## Known limitations

- Karpenter logic is covered by unit tests against mocked CRDs. The Karpenter v1
  CRD coordinates and `amiSelectorTerms` classification are documentation-derived
  — **verify against a real `EC2NodeClass` before relying on it in production.**
- Pinned Karpenter selectors (`id` / `name` / `tags`) are **warned about, not
  rewritten** — update their `amiSelectorTerms` manually to upgrade those nodes.
- `--force` deletes pods bypassing PodDisruptionBudgets (this is inherent to
  `--force`).
- The self-managed ASG path still uses the deprecated `CreateLaunchConfiguration`
  API (migration to Launch Templates is planned).

## License

Licensed under the MIT-0 License (inherited from the upstream project).

This project is a community fork of `aws-samples/eks-cluster-upgrade` and is not
an AWS service. Support is best-effort via the
[Issues](https://github.com/namejsjeongkr/eksupgrade/issues) section.
