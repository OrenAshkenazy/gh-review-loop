# Compliance notes

These are the parts of GLG's network-member terms that show up inside questionnaires and project emails. Read them so the hard rules in SKILL.md make sense, and so you recognize a question that must go to the member.

## The AI clause

Verbatim from GLG project emails: "Other than as integrated and utilized by GLG in its systems, Network Members may not use or rely upon AI-generated content when working or seeking to work with GLG clients, absent explicit written permission from GLG. This prohibition against using AI applies across all engagements, including but not limited to during participation in phone consultations or surveys, drafting written work product, or providing written or verbal responses to GLG project opportunities through our systems or on calls with our employees."

What this skill does about it: the agent generates no answer content. It reuses what the member already told GLG (seeded from GLG's own confirmation emails), what the member wrote in their profile, and what the member types during the session. Whether that form-filling is acceptable to GLG is the member's call and the member's risk; the skill makes it easy to ask GLG for the written permission the clause mentions (networkmembersupport@glgroup.com) and to stop using the skill if they say no. If the member asks you to "just answer it however", decline that part and explain why in one line.

## Questions that always go to the member

- Anything about a current employer, including "are you currently employed by", "may we contact", or a list of companies with "do you work for any of these".
- Conflict of interest, non-public information, pending trials, government roles, public-company employment within six months. Client-appended representation blocks (see `flags.client_terms_appended`) bundle several of these into the accept click.
- Consent to recording (`flags.recorded`): accepting the project is the consent.
- Single-blinded projects (`flags.single_blinded`): the member decides whether they can clear conflicts without the client's name.
- "Which other GLG projects have you done" style questions: GLG's own confidentiality section forbids discussing other GLG projects, so the answer is usually none, but it is the member's answer.

## What the member may not disclose, so never put it on a sheet

- Confidential or proprietary information about current or former employers (GLG presumes the duty lasts six months after leaving).
- Material non-public information about any company.
- The identity of GLG clients from past projects, the topics discussed, or how often the member works with GLG.

If a bank entry or profile line would disclose one of these, do not reuse it; flag it to the member instead.

## Cancellation and rate notes (context only)

The member's standing rate and any cancellation terms are between the member and GLG network member support. Do not negotiate rate inside a questionnaire; if a project email quotes a different rate than the profile, show both in triage and let the member decide before accepting.
