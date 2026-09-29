# Phase 06 — Bedrock Guardrails configuration

## Resources and rollout

`cloudformation/phase-06-guardrails.yaml` defines only an `AWS::Bedrock::Guardrail`
and an `AWS::Bedrock::GuardrailVersion`. It does not create IAM roles, log
groups, buckets, or other services. The deployment identity should be limited
to the CloudFormation/Bedrock operations needed to manage these two resources.
The application runtime separately needs only `bedrock:ApplyGuardrail` on this
guardrail ARN; do not grant a wildcard resource.

The template publishes an immutable version. When changing policy settings,
change `GuardrailVersionDescription` (for example, from `Phase 06 baseline v1`
to `Phase 06 baseline v2`) so CloudFormation creates a new version. Point the
application to the output `GuardrailId` and `GuardrailVersion` together. The
template intentionally does not enable cross-Region routing; verify model and
Guardrails availability in the selected region before a deployment.

## Initial policy and expected behavior

| Policy | Initial configuration | Expected behavior |
| --- | --- | --- |
| Prompt attacks | `PROMPT_ATTACK`, HIGH on input, BLOCK; output disabled (`NONE`) | Reject jailbreak or prompt injection detected in the user's question. Prompt attacks are user-input threats; this policy is not applied to generated output. |
| Harmful/abusive content | HATE, INSULTS, SEXUAL, VIOLENCE, MISCONDUCT at HIGH, BLOCK on input/output | Block content classified in these categories. Sexual-content filtering can match legitimate legal material, so test its false positives. This does not attempt to prohibit ordinary disagreement or legal allegations. |
| Personal identifiers | NAME, EMAIL, PHONE, ADDRESS anonymized on input/output | Replace detected values with entity markers; the application must continue only with the returned masked text. |
| High-risk secrets | SSN, payment card number, AWS keys, passwords blocked | Reject detected credentials and financial identifiers. |
| Individualized legal advice | A narrow denied topic for personalized recommendations, legal strategies, or outcome predictions | Block requests for a decision tailored to a person's facts, while allowing general legal information and explanations of supplied documents. |
| Contextual grounding | GROUNDING 0.75 and RELEVANCE 0.50, enabled with BLOCK | Block output that falls below either initial score threshold when evaluated with its query and authorized retrieved passages. |

The grounding thresholds are starting values for evaluation, not validated
production thresholds. Tune them against fictional expected/actual cases and
record false positives and misses before production. Contextual grounding is an
output check: the integration must supply the response, the query, and the
authorized source passages with the Bedrock-required qualifiers. It does not
replace citation-ID validation.

Authorization is deterministic and remains upstream. Resolve the verified
actor's tenant/matter membership before retrieval or any guardrail call. A
cross-matter request must fail authorization even if Guardrails returns no
intervention. Guardrails classifiers are probabilistic: they can miss attacks,
PII, prohibited advice, or unsupported claims, and can block benign material.
They are a defense-in-depth content control, not an access-control boundary or
a substitute for server-side validation.

Record only safe outcome metadata such as correlation ID, input/output stage,
intervention action, and policy categories. Do not log prompts, source passages,
PII matches, document content, or secrets.

## Cost and representative AWS validation

Bedrock Guardrails evaluations for this versioned guardrail and the planned
`ApplyGuardrail` path are metered per configured safeguard and text unit (up to
1,000 characters). When checked, the main Guardrails pricing table lists text
content filters (including `PROMPT_ATTACK`) at $0.15 per 1,000 text units,
denied topics at $0.15, sensitive-information filters at $0.10, and contextual
grounding at $0.10. Grounding units combine source, query, and response. These
are the current reference rates for the guardrail-resource path documented
here. Rates can change, so reconfirm them before deployment and a real run.
Charges accrue for enabled safeguards on the checked input/output, and policy
types add together. An output intervention can still incur the model
inference cost because the model has already generated that response.

Local unit tests and template checks are the default. A representative AWS
smoke should be separately authorized before deployment or invocation; keep
it small and use synthetic text only. After creating this stack, make one
expected-safe and one expected-blocked `ApplyGuardrail` case for input attacks,
PII, and the denied topic, plus one grounded and one unsupported output case.
Capture only action/category/score outcomes, compare expected vs actual, then
delete the stack and verify teardown:

```powershell
aws cloudformation delete-stack --stack-name LegalDeskPhase06Guardrails --region eu-west-1
aws cloudformation wait stack-delete-complete --stack-name LegalDeskPhase06Guardrails --region eu-west-1
```

Do not run the full adversarial suite against AWS. No AWS resource has been
deployed and no Guardrails API has been invoked for this local implementation.

## AWS references

- [AWS::Bedrock::Guardrail CloudFormation resource](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-bedrock-guardrail.html)
- [AWS::Bedrock::GuardrailVersion CloudFormation resource](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-bedrock-guardrailversion.html)
- [Detect prompt attacks with Amazon Bedrock Guardrails](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-prompt-attack.html)
- [Bedrock Guardrails contextual grounding](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-contextual-grounding-check.html)
- [Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/)
