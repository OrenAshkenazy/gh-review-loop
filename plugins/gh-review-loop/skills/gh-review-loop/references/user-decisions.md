# User questions and decisions

Load when an existing reviewer, verification-profile, or judge-mode flow asks for a choice, or when a finding needs the user's decision. Preserve those flows' options and saved selections; do not ask again for a decision already supplied in this conversation.

## Asking and waiting

Prefer the current runtime's user-question tool when available and permitted in the current mode. Examples include `request_user_input`, `request_user_input_async`, `AskUserQuestion`, and `ask_user`; use only a tool actually exposed to this agent and follow its schema and mode restrictions. Otherwise ask in chat. Do not change modes merely to obtain a question tool.

For a finding, include its GitHub thread URL, the proposed change, why existing instructions or code do not settle the decision, and concrete choices. Investigate clear bugs, false positives, and duplicates from evidence; reserve questions for product, compatibility, security-policy, or conflicting-requirement choices. An optional judge's `needs_human` verdict is another reason to ask.

Wait for the actual answer before applying dependent changes. A blocking tool waits internally; an asynchronous tool's return only means the question was submitted. While unanswered, pause PR publication and re-review requests; safe independent work may continue. Dismissal, timeout, an empty response, a preselected option, and silence are not decisions.

Use conversation history for the pending question and answer. If the user is away or the host interrupts the wait, report what remains pending and resume when they reply. Do not add decision-state files, poll indefinitely, or select a default on their behalf. On resumption, refresh PR and thread data before acting; a choice about superseded feedback does not authorize acting on new feedback. Follow the normal terminal receipt policy if the run ends, and keep the existing re-review cap when resuming.

Apply the user's selected behavior within the existing scope, or provide an explanation/reply draft if that is what they requested. Choosing to keep current behavior is not automatically a deferral. Offer the explicit "Defer and record on GitHub" choice when appropriate, making clear that it posts a reply and leaves the thread unresolved.

## Recording a deferral

Follow [GitHub write safety](write-safety.md#resolution-policy). Selecting "Defer and record on GitHub", or explicitly requesting the same action in chat, authorizes the agent to post the marker for the identified thread; the user need not open GitHub. No answer, a generic acknowledgment, or merely requesting an explanation authorizes no deferral reply.

1. Refresh the existing inline thread before posting. If it was resolved or became outdated, report its current state instead. If the latest selected-reviewer feedback changed since the question, ask about that feedback before deferring it. Review-body findings without an inline thread cannot use this marker.
2. Use authenticated `gh` to post a reply to an existing inline review comment in that specific thread, not a top-level PR comment. The reply's exact first line must be `/review-loop defer`, with no indentation, quoting, backticks, or additional text on that line. Put the user's reason on following lines if supplied.
3. Confirm from fresh thread data that the reply satisfies the existing deferral policy: author association `OWNER` or `COLLABORATOR`, non-bot account, and marker after the latest selected-reviewer comment with no later eligible reply clearing it. A successful POST alone does not prove the loop recognizes the deferral. If posting fails, the account does not qualify, or recognition cannot be confirmed, report that deferral was not established; do not claim the thread is excluded or retry blindly.
4. Leave the GitHub thread unresolved. Refresh through the bundled fetcher and continue with other actionable threads within the cap. The marker defers the whole thread, not an individual sentence in it. A later reviewer comment or a later owner/collaborator reply without the marker makes it actionable again.

GitHub supplies the thread context, so the marker takes no finding-ID argument. Existing scripts already recognize it; no new Python persistence mechanism is needed.
