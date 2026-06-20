"""
Reproduce the README claim:
    Code search (100 results): 17,765 -> 1,408 = 92% savings

Use a realistic Anthropic-format agent conversation:
  system → user (asks for search) → assistant (tool_use) → user (tool_result).

The bulk of the tokens live in the tool_result block — that's the tool
output that should be compressed.
"""
from headroom import compress
import json

SAMPLE_SNIPPET = """\
def authenticate_request(request: Request) -> Optional[User]:
    \"\"\"Verify the bearer token and return the authenticated user.

    Falls back to anonymous when no token is present so that public
    endpoints continue to work. Raises AuthError on invalid tokens.
    \"\"\"
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        raise AuthError("Empty bearer token")
    return verify_token(token)
"""

def build_messages(num_results: int = 100):
    snippets = []
    for i in range(num_results):
        mod = i % 7
        paths = [
            "src/api/auth.py", "src/api/users.py", "src/services/payments.py",
            "src/services/notifications.py", "src/utils/caching.py",
            "src/utils/serialization.py", "tests/integration/test_auth.py",
        ]
        snippets.append({
            "path": paths[mod],
            "line": 42 + (i % 200),
            "match": "authenticate_request",
            "snippet": SAMPLE_SNIPPET.strip(),
        })

    tool_output_json = json.dumps({
        "query": "authenticate_request",
        "total": num_results,
        "results": snippets,
    }, indent=2)

    return [
        {"role": "system",
         "content": "You are a code-search assistant running inside an "
                    "agent. Tool results are returned to you as JSON. "
                    "Summarize the matches and answer the user's question."},
        {"role": "user",
         "content": "Search the codebase for `authenticate_request`."},
        {"role": "assistant",
         "content": [{
             "type": "tool_use",
             "id": "toolu_001",
             "name": "code_search",
             "input": {"query": "authenticate_request", "limit": 100},
         }]},
        {"role": "user",
         "content": [{
             "type": "tool_result",
             "tool_use_id": "toolu_001",
             "content": tool_output_json,
         }]},
        {"role": "user",
         "content": "Summarize what authenticate_request does and list the files."},
    ]


def main():
    msgs = build_messages(100)
    # Two runs: default (user-message protection ON) vs aggressive
    for label, kwargs in [
        ("default (protect user messages)", {}),
        ("aggressive (compress_user_messages=True)", {"compress_user_messages": True}),
    ]:
        r = compress(msgs, model="claude-sonnet-4-5-20250929", optimize=True, **kwargs)
        b, a = r.tokens_before, r.tokens_after
        pct = (1 - a / b) * 100 if b else 0.0
        print(f"  {label}")
        print(f"    {b:>6,} -> {a:>6,} = {pct:.1f}%  "
              f"transforms={r.transforms_applied}  ratio={r.compression_ratio:.3f}")
    print("\nREADME claim: 17,765 -> 1,408 = 92%")


if __name__ == "__main__":
    main()