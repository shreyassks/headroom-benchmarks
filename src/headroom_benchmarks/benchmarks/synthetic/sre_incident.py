"""
Reproduce the README claim:
    SRE incident debugging: 65,694 -> 5,118 = 92% savings

Build a synthetic large tool-output payload mimicking what an SRE on-call
agent sees: stack trace + Kubernetes describe + multiple service logs.
"""
from headroom import compress
import json
import random

random.seed(42)

def make_stacktrace(depth: int = 30) -> str:
    frames = []
    for i in range(depth):
        frames.append(
            f"  at com.acme.payments.PaymentService.charge "
            f"(PaymentService.java:{142 + i * 7})\n"
            f"  at com.acme.payments.PaymentController.handle "
            f"(PaymentController.java:{88 + i * 3})\n"
            f"  at jdk.internal.reflect.NativeMethodAccessorImpl.invoke0 "
            f"(Native Method)"
        )
    return (
        "java.lang.RuntimeException: Charge failed: upstream timeout after 30000ms\n"
        + "\n".join(frames)
        + "\n\t... 147 more\n"
    )

def make_k8s_describe() -> str:
    pods = []
    for i in range(60):
        pods.append({
            "name": f"payments-api-{i:03d}",
            "ready": "0/1" if i < 12 else "1/1",
            "status": "CrashLoopBackOff" if i < 12 else "Running",
            "restarts": random.randint(8, 40) if i < 12 else random.randint(0, 3),
            "age": f"{random.randint(2, 48)}h",
            "node": f"ip-10-0-{random.randint(1,9)}-{random.randint(10,99)}.ec2.internal",
        })
    return json.dumps({"items": pods}, indent=2)

def make_logs() -> str:
    lines = []
    for i in range(400):
        lines.append(
            f"2026-06-19T14:0{random.randint(0,5)}:{random.randint(10,59)}Z "
            f"ERROR [payments-api] upstream timeout after 30000ms "
            f"req_id=req-{random.randint(100000, 999999)} "
            f"user_id=u-{random.randint(1, 50000)} "
            f"amount={random.randint(1, 999)}.00 "
            f"gateway=stripe attempt={random.randint(1, 5)}"
        )
    return "\n".join(lines)

def build_messages() -> list:
    payload = json.dumps({
        "query": "Why are payments-api pods crash-looping?",
        "stack_trace": make_stacktrace(40),
        "k8s_describe": make_k8s_describe(),
        "logs": make_logs(),
    }, indent=2)
    return [
        {"role": "system", "content": "You are an SRE agent. Analyze "
         "tool output and identify the root cause."},
        {"role": "user", "content": "Why are payments-api pods crash-looping?"},
        {"role": "assistant", "content": [{
            "type": "tool_use", "id": "toolu_002",
            "name": "incident_brief",
            "input": {"service": "payments-api"},
        }]},
        {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "toolu_002",
            "content": payload,
        }]},
        {"role": "user", "content": "Diagnose the incident and propose a fix."},
    ]


def main():
    msgs = build_messages()
    r = compress(msgs, model="claude-sonnet-4-5-20250929", optimize=True)
    b, a = r.tokens_before, r.tokens_after
    pct = (1 - a / b) * 100 if b else 0.0
    print(f"README claim: 65,694 -> 5,118 = 92%")
    print(f"This run:    {b:>7,} -> {a:>7,} = {pct:.1f}%")
    print(f"Transforms applied: {r.transforms_applied}")
    print(f"Compression ratio: {r.compression_ratio:.3f}")


if __name__ == "__main__":
    main()