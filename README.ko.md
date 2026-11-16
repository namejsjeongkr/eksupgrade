# Amazon EKS 업그레이드 유틸리티 (Karpenter 지원 fork)

🌐 **언어**: [English](README.md) · **한국어**

`eksupgrade`는 Amazon EKS 클러스터 업그레이드 과정을 자동화하는 CLI 도구입니다 —
컨트롤 플레인, 관리형 애드온, 그리고 **Cluster Autoscaler·self-managed ASG·Karpenter**
로 관리되는 워커 노드를 모두 다룹니다.

> **이 fork에 대하여.** 이 저장소는 더 이상 활발히 유지보수되지 않는
> [`aws-samples/eks-cluster-upgrade`](https://github.com/aws-samples/eks-cluster-upgrade)
> 를 fork한 것에서 출발했으며, 현재는 독립 프로젝트입니다. 가장 큰 변화는
> 인스턴스를 종료하는 대신 **Karpenter 네이티브 drift 기반 노드 업그레이드**를
> 채택한 점이며, 함께 Cluster Autoscaler 지원을 복원하고 더 안전한 노드 drain을
> 제공합니다. [업스트림과의 차이점](#업스트림과의-차이점)을 참고하세요.

## 업스트림과의 차이점

- **Karpenter 노드는 종료가 아니라 Drift로 업그레이드합니다.** Karpenter
  컨트롤러는 **계속 실행 상태**로 둡니다. 컨트롤 플레인 업그레이드 후 alias 기반
  `EC2NodeClass` selector가 새 Kubernetes 버전의 AMI로 재해석되고, Karpenter가
  PodDisruptionBudget·disruption budget·`karpenter.sh/do-not-disrupt`를 존중하며
  **용량 우선(capacity-first)** 으로 노드를 교체합니다. 기존 방식(컨트롤러 정지 →
  EC2 종료)은 용량 공백을 만들고, PDB를 우회했으며, 실제로 AMI를 갱신하지도
  못했습니다.
- **두 autoscaler를 나란히 지원합니다.** Cluster Autoscaler는 (label selector로
  Helm 설치명까지 포함해) 감지되어 일시 정지/재개되며, Karpenter 경로는 별개로
  동작하고 CA 의미론을 적용하지 않습니다.
- **소유자 인식 노드 drain.** drain은 DaemonSet과 static/mirror pod를 건너뛰며,
  관리되지 않는 pod가 유실될 상황이면(`--force` 없이는) 시작 자체를 거부하여 노드가
  절반만 비워지는 일이 없습니다.

## 클러스터 업그레이드

업그레이드는 각 단계에서 클러스터를 안정적으로 유지하기 위해 다음 순서로 진행됩니다:

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

단계 요약:

1. **컨트롤 플레인 업그레이드** — AWS가 업그레이드를 수행하고, eksupgrade는 ACTIVE가
   될 때까지 대기합니다. (alias 기반 EC2NodeClass의 Karpenter drift가 여기서
   시작됩니다.)
2. **애드온 업그레이드** (vpc-cni → kube-proxy → coredns) — 버전은 클러스터 버전에
   맞춰 EKS API에서 실시간으로 해석되며, vpc-cni는 마이너 버전 단위로 단계적으로
   업그레이드됩니다.
3. **Cluster Autoscaler 일시 정지** (Karpenter는 계속 실행) — CA 디플로이먼트를 0으로
   스케일하여 노드 교체를 방해하지 못하게 합니다. Karpenter는 drift로 노드를 교체해야
   하므로 계속 실행 상태를 유지합니다.
4. **관리형 노드 그룹 업그레이드** — EKS가 롤링 AMI 교체를 수행합니다(`--parallel`
   선택 가능).
5. **self-managed 노드 그룹 업그레이드** (ASG 기반) — 각 Auto Scaling Group에 대해:
   AMI 타입 감지(AL2023/AL2/Bottlerocket/Windows/Ubuntu) → SSM에서 최신 EKS 최적화
   AMI 조회 → 오래된 인스턴스마다: 교체 인스턴스 기동 → cordon → 소유자 인식
   drain(`--force` 없으면 PDB 존중) → 종료.
6. **Karpenter 노드 업그레이드 — Drift 방식** (Karpenter가 감지된 경우) — 각
   EC2NodeClass를 검사: alias selector(예: `al2023@latest` / `al2023@vX`)는 새
   Kubernetes 버전 AMI로 자동 drift되며 도구는 관찰·대기만 합니다. `id`/`name`/`tags`
   selector는 자동 drift되지 **않으며**, 도구는 해당 노드가 수동 `amiSelectorTerms`
   갱신이 필요하다고 경고합니다. 이후 drift 중인 NodePool의 노드가 목표 버전이 될
   때까지(상한 있는) 대기합니다. 컨트롤러는 절대 정지/강제되지 않습니다.
7. **Cluster Autoscaler 재개** — 원래 replica 수로 복원합니다(실패 시에도). Karpenter는
   정지된 적이 없으므로 재개가 필요 없습니다.

### 지원 노드 타입

| 노드 타입 | 관리형 노드 그룹 | self-managed (ASG) | Karpenter |
|-----------|:-----------------:|:------------------:|:---------:|
| Amazon Linux 2023 (AL2023) | ✅ | ✅ | ✅ (drift) |
| Bottlerocket | ✅ | ✅ | ✅ (drift) |
| Windows Server | ✅ | ✅ | ✅ (drift) |
| Ubuntu | ✅ | ✅ | ✅ (drift) |
| Amazon Linux 2 (AL2) | ✅ | ✅ | 해당 없음¹ |

¹ AL2 EKS 최적화 AMI는 지원이 종료되었습니다. EKS 1.32가 이를 제공한 마지막
버전이며, 1.33 이상에서는 AL2023을 사용하세요.

### 지원 Kubernetes 버전

순차적 단일 마이너 업그레이드만 지원합니다(예: `1.34 → 1.35`). EKS는 한 번에 여러
마이너를 건너뛰는 것을 허용하지 않으므로, 1.34에서 1.36으로 가려면 도구를 두 번
실행하세요(`1.34 → 1.35` 후 `1.35 → 1.36`).

> EKS 표준 지원은 현재 대략 1.33–1.36 범위입니다(일부 구버전은 확장 지원). 이 도구는
> 애드온 버전을 EKS API에서 실시간으로 해석하므로, 코드에 박힌 표가 아니라 EKS가 현재
> 제공하는 버전을 그대로 따릅니다.

## 사전 요구사항

AWS와 Kubernetes 클러스터 양쪽에 대한 권한이 필요합니다.

1. 소스에서 설치합니다(이 fork는 PyPI에 게시되지 않았습니다).

   **권장 — [pipx](https://pipx.pypa.io)로 `eksupgrade` 명령 설치**
   (격리 설치되며 `eksupgrade`가 `PATH`에 등록됩니다):

```sh
git clone https://github.com/namejsjeongkr/eksupgrade.git
pipx install ./eksupgrade
eksupgrade --help
```

   이후 새 변경사항을 받은 뒤 갱신하려면: `pipx install --force ./eksupgrade`.
   (자주 코드를 수정한다면 `pipx install --editable ./eksupgrade`로 설치하면
   `git pull`만으로 재설치 없이 최신 코드가 반영됩니다.)

   **대안 — 개발용**으로는 clone한 저장소 안에서 [uv](https://docs.astral.sh/uv/)를
   사용합니다(Python 3.12 이상). 이 경우 명령은 `uv run eksupgrade ...` 형태로 실행합니다:

```sh
git clone https://github.com/namejsjeongkr/eksupgrade.git
cd eksupgrade
uv sync
uv run eksupgrade --help
```

2. AWS 권한 — 최소 정책 예시:

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

3. 클러스터 인증을 위해 로컬 kubeconfig를 갱신합니다:

```sh
aws eks update-kubeconfig --name <CLUSTER-NAME> --region <REGION>
```

## 사용법

> 아래 예시는 `eksupgrade` 명령(pipx 설치)을 사용합니다. 개발용 uv로 설치했다면
> 각 명령 앞에 `uv run`을 붙이세요.

```sh
eksupgrade --help
```

```sh
 Usage: eksupgrade [OPTIONS] CLUSTER_NAME CLUSTER_VERSION REGION

 Run eksupgrade against a target cluster.

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
   --version                   Show the version and exit
   --help                      Show this message and exit
```

예시:

```sh
eksupgrade my-cluster 1.35 ap-northeast-2
```

### 읽기 전용 사전 점검(preflight)

아무것도 변경하지 않고 읽기 전용 평가를 실행합니다:

```sh
eksupgrade <cluster> <target-version> <region> --preflight --no-interactive
```

컨트롤 플레인, 애드온, 관리형 노드 그룹, Karpenter, 그리고 PodDisruptionBudget
커버리지(replicas≥2인데 PDB가 없는 워크로드를 경고)를 점검하고, 요약 리포트를 출력한
뒤 어떤 업그레이드도 수행하지 않고 종료합니다. 종료 코드: `0` 안전(경고는 허용), `1`
차단 이슈 발견, `2` 점검 자체를 수행할 수 없음.

## 알려진 한계

- Karpenter 로직은 mock된 CRD에 대한 단위 테스트로 검증됩니다. Karpenter v1 CRD 좌표와
  `amiSelectorTerms` 분류는 문서 기반으로 작성되었으므로 — **프로덕션에서 의존하기 전에
  실제 `EC2NodeClass`로 검증하세요.**
- 고정형(pinned) Karpenter selector(`id`/`name`/`tags`)는 **재작성되지 않고 경고만
  합니다** — 해당 노드를 업그레이드하려면 `amiSelectorTerms`를 수동으로 갱신하세요.
- `--force`는 PodDisruptionBudget을 우회하여 pod를 삭제합니다(`--force`의 본질적
  동작입니다).
- self-managed ASG 경로는 아직 deprecated된 `CreateLaunchConfiguration` API를
  사용합니다(Launch Template으로의 마이그레이션 예정).

## 라이선스

MIT-0 라이선스를 따릅니다(업스트림 프로젝트에서 상속).

이 프로젝트는 `aws-samples/eks-cluster-upgrade`의 커뮤니티 fork이며 AWS 서비스가
아닙니다. 지원은 [Issues](https://github.com/namejsjeongkr/eksupgrade/issues) 섹션을
통해 최선의 노력(best-effort)으로 제공됩니다.
