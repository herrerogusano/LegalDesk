# Phase 15 T9 — searchable scans: F-31 options study

Evidence checked **2026-11-04, approximately 17:03–17:16 UTC**, against repository
`18eec63`, account `344774635844`, Region `eu-west-1`. Documentation only. No
template, application, dataset, threshold or shared-vault change. No upload,
ingestion, model/Harness invocation, Retrieve, vector read/write, deployment or
data-source update was performed. The existing default `aws login` identity was
verified to be `aws-cloud-ai-web-cli`; there is no profile with that name. No
credentials, user identifiers or subjects are recorded here.

## Recommendation and owner decision

**Make scanned pages searchable, using literal page text already acquired by
IDP/Textract, with a trusted mapping back to each original PDF.** Keep the current
parser for digital PDFs. Add only the missing scanned pages to a separate,
default-parser derived-text source, and implement the page/source/status mapping
before treating those documents as indexed. This avoids buying the same OCR
twice, keeps digital evidence comparatively stable, and makes it possible to
check exactly what text supports a legal answer. It requires application work;
copying text files into S3 alone would be incorrect.

For a legal product, a successful upload that leaves important pages invisible
to search is a material limitation. IDP fields cannot substitute for the clauses
needed to answer a free-text question. OCR can still misread a deadline, amount
or negation: the original page must remain the evidence, and uncertainty must
remain visible.

If the owner wants the smallest application change instead, **BDA is the managed
alternative to test first**, with an approved migration and citation canary. Its
page price is predictable and Spanish/English support is documented. A foundation
model parser offers more flexibility but introduces token-cost uncertainty and
another generative transformation of legal text. Neither is a harmless switch
on this deployed data source.

The owner must choose: (a) authorize a separate implementation and bounded paid
validation of searchable scans, preferably the derived-page route, or (b) accept
the product limitation and preserve the resulting golden failures. This study
authorizes neither implementation nor spending. F-32 remains a separate fix.

| Option | What it costs, before shared indexing costs | What changes | What could go wrong |
|---|---|---|---|
| 1. BDA KB parser | $0.01 per PDF page; B $0.44, full manifest $0.45; matter A adds $0.01 × its pages | Replace source; documented multimodal route also needs a new KB/storage destination. Reparse digital PDFs too | OCR/layout errors, generated captions, citation/page differences, regional documentation conflict, cutover or partial-ingestion failure |
| 2. FM KB parser, Nova Pro example | Estimated $0.005428/page typical, $0.02576/page planning upper; B $0.238832 / $1.13344 | Replace source; multimodal KB migration as above; all PDFs go through a model | Omitted/rewritten legal wording, prompt injection at parsing time, variable tokens/throttling, source/page/citation drift |
| 3. IDP-derived page text, recommended | $0 extra OCR if verified artifacts exist; replacement OCR $0.0015 per processed PDF page if needed; indexing still costs | New page objects/sidecars and a source, trusted original/page mapping, logical indexing status and cleanup support | Missing/stale artifacts, wrong page mapping, original/derived duplication, asynchronous race, incomplete governance integration |
| 4. Accept limitation | $0 incremental parser/OCR migration cost | Document the limitation and preserve current corpus/parser | Known unreachable evidence; frozen retrieval gate cannot pass even under an otherwise perfect result |

These amounts are **not measured bills or hard spending caps**. Details and
matter-A scenarios follow. No option guarantees a passing golden run.

## Evidence: corpus, failures and mixed pages

Read locally: `C:\Users\herre\legaldesk-t9\corpus-b-20261104\corpus\corpus-manifest.json`
and the upload journal. All **27/27 file SHA-256 values match** the manifest.
Manifest SHA-256: `f9694ef89716e51253a7a7bd3b1a4d7d9a8d0346e7543395b9add5f6455f9767`.
`pypdf` counted pages and identified pages with empty extracted text; this is a
local text-layer check, not an OCR-quality or KB-coverage test.

| Population | Files | Valid PDF pages | Pages without a text layer |
|---|---:|---:|---:|
| Full manifest | 27: 26 valid + 1 deliberately truncated | **45** | **12** |
| Uploaded primary corpus, matter B | 26: 25 valid + 1 truncated | **44** | **12** |
| Glacier, deliberately not uploaded | 1 valid | 1 | 0 |
| Matter A, outside this manifest | 5 sources seen by the ingestion job | **Unverified** | At least one image-only document fails; page count unverified |

The requested 45/12 counts are correct **for the manifest**, not for the uploaded
B population alone. Do not upload Glacier or count it as live to reconcile the
numbers. The truncated file has no valid page count; parser attempts/failures
cannot be promised free.

The six wholly scanned B documents are one page each:

- `doc-contract-02-es-scanned-leapday`
- `doc-demand-02-es-scanned`
- `doc-demand-05-en-scanned`
- `doc-judgment-02-es-scanned`
- `doc-judgment-05-en-scanned-optional-absent`
- `doc-ext-notice-02-en-scanned-noisy`

The other six scanned pages are page 2 of `doc-contract-03-en-mixed-nda`,
`doc-demand-03-en-mixed`, and `doc-judgment-03-en-mixed-interpretive`, and pages
**5, 6, 7** of `doc-ext-lease-01-es-long`. The last has seven pages in total;
`lt-19` needs page **6**. `doc-contract-05-en-mixed-explicit-conflict` has two
pages with extractable text; its name alone is not evidence of a missing page.

Live `GetKnowledgeBaseDocuments`, in explicit batches of at most ten journal
identifiers, returned **19 INDEXED / 7 NOT_FOUND** for B: exactly those six scans
and `doc-failed-upload` are NOT_FOUND. All four mixed PDFs listed above are
INDEXED. `ListKnowledgeBaseDocuments` returned four INDEXED A sources; explicitly
querying A's fifth source from the job failure returned NOT_FOUND. A list of
indexed records alone would have missed it.

Live `GetIngestionJob` for `IJHTJARJT9` returned COMPLETE, from
16:38:53.617 to 16:39:01.664 UTC (**8.047 seconds**), with 31 documents and 31
metadata files scanned, 19 new indexed, 8 failed, zero deleted/modified/skipped.
Its failure message contains several groups: **3 + 1 + 3 no-text files** and **1
unparseable file**. The final no-text group includes A's fifth document. The
initial “3 no text / 1 unparseable” excerpt is not the whole job. COMPLETE does
not mean every document succeeded.

**Mixed scanned-page searchability remains unverified live.** No chunk-count API
was found in the KB document-status interface. S3 Vectors has ListVectors and
GetVectors, but free use was not established: the current Irish catalog has
nonzero vector request tiers, and AWS describes applicable read-request charges.
They were not called. No Retrieve was called because it invokes embeddings.
See [S3 pricing](https://aws.amazon.com/s3/pricing/),
[S3 FAQ](https://aws.amazon.com/s3/faqs/) and
[ListVectors API](https://docs.aws.amazon.com/AmazonS3/latest/API/API_S3VectorBuckets_ListVectors.html).
The default parser only handles existing text; therefore missing scanned pages
are the strong technical expectation, not a measured vector-store result.
INDEXED proves document acceptance, not complete page coverage.

## Live configuration and availability

KB `AQWCFL7239` is ACTIVE; source `XIA81RBIPG` is AVAILABLE, with no
`parsingConfiguration`. Source bucket is
`legaldeskphase02documents-documentbucket-ojlu4kvhlied`; inclusion prefix is
`tenants/tnt_phase14_20260928/`. Deletion policy is **DELETE**. Chunking is
FIXED_SIZE, **800 tokens, 15% overlap**. Embeddings are
`amazon.titan-embed-text-v2:0`, 1024 FLOAT32 dimensions. Vector index:
`arn:aws:s3vectors:eu-west-1:344774635844:bucket/legaldeskphase03knowledgebas-legaldeskvectorbucket-wepqt0lcpc4n/index/legaldesk-phase03`.
The KB has **no supplemental-data storage configuration**.

| Option | Irish availability and account evidence | CloudFormation consequence |
|---|---|---|
| BDA | AWS's July 2025 regional announcement and August language announcement include Ireland and use as a KB parser. Live GetDataAutomationProject of `arn:aws:bedrock:eu-west-1:aws:data-automation-project/public-rag-default` succeeds: stage LIVE, status COMPLETED. No BDA ingestion or invocation was tested | BedrockDataAutomationConfiguration exists in the live Irish DataSource schema. Adding ParsingConfiguration replaces the source |
| FM | Nova vision parsers and inference profiles are documented. Live `eu.amazon.nova-pro-v1:0` is ACTIVE. GetFoundationModelAvailability for `amazon.nova-pro-v1:0`: agreement AVAILABLE, AUTHORIZED, entitlement AVAILABLE, Region AVAILABLE. Actual parser invocation untested; current KB role cannot invoke it | BedrockFoundationModelConfiguration exists in the live schema; parser addition replaces the source |
| Derived text | S3/default parser and Titan are already deployed here; Textract asynchronous OCR is already integrated in IDP | A new default-parser source can be added to the existing KB without changing the existing source's parsing configuration. IAM/source-prefix and application changes still require review |
| Accept | Already deployed | No replacement or IAM change |

Sources: [BDA regional expansion](https://aws.amazon.com/about-aws/whats-new/2025/07/amazon-bedrock-data-automation-additional-aws-regions/),
[BDA Spanish support](https://aws.amazon.com/about-aws/whats-new/2025/08/amazon-bedrock-data-automation-five-additional-languages-documents/),
[KB parser/model support](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-supported.html),
[FM parser schema](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-bedrock-datasource-bedrockfoundationmodelconfiguration.html),
[BDA parser schema](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-bedrock-datasource-bedrockdataautomationconfiguration.html).

**Documentation conflict:** the current KB support page still says BDA parsing is
Oregon-only/preview, whereas newer regional/language announcements describe
Ireland GA and KB use. The live Irish public RAG project corroborates regional
service availability, not end-to-end ingestion compatibility with this KB. Treat
Irish BDA KB ingestion as requiring an approved canary or AWS clarification before
cutover. Do not migrate to Oregon to evade this uncertainty.

The Nova profile ARN is
`arn:aws:bedrock:eu-west-1:344774635844:inference-profile/eu.amazon.nova-pro-v1:0`.
Live GetInferenceProfile names exactly four model destinations: `eu-west-1`,
`eu-west-3`, `eu-central-1`, `eu-north-1`. A profile's availability does not prove
KB-role authorization, absence of SCP restrictions or inference success.

Claude 3.5 Sonnet v1/v2 and 3.7 Sonnet GetFoundationModelAvailability reads return
AUTHORIZED, entitlement/Region AVAILABLE, but **agreement NOT_AVAILABLE**. No
matching 3.5/3.7 profile was returned by ListInferenceProfiles. These are not
verified usable parser choices in this account. Newer Sonnet profiles are listed,
but their agreement/access and parsing behavior were not investigated; no silent
substitution is proposed. Nova Pro is the fully checked availability example.

BDA requires geographic cross-Region processing: Ireland's documented EU profile
is `arn:aws:bedrock:eu-west-1:344774635844:data-automation-profile/eu.data-automation-v1`,
with destinations Frankfurt, Stockholm, Milan, Spain, Ireland and Paris. This is
separate from an FM inference profile; listing FM profiles does not verify BDA
permissions. No global profile is proposed.
[BDA cross-Region requirements](https://docs.aws.amazon.com/bedrock/latest/userguide/bda-cris.html).

## Prices and one-off/ongoing cost

USD gross, before tax, credits or free allowances. Current **EU (Ireland)**
regional AWS Price List catalogs were downloaded anonymously with Python
`urllib`; web extraction of these JSON URLs failed, but the downloads succeeded.
This used no Cost Explorer API. Catalog versions/publication dates:

- [AmazonBedrock Irish catalog](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/current/eu-west-1/index.json): `20261102165015`, 2026-11-02.
- [AmazonTextract Irish catalog](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonTextract/current/eu-west-1/index.json): `20261027184301`, 2026-10-27.
- [AmazonS3 Irish catalog](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/eu-west-1/index.json): `20261015173540`, 2026-10-15.

| Usage dimension / SKU where material | Verified Irish unit price |
|---|---:|
| BDA Documents Standard, `EU-DataAutomation-Standard-PagesProcessed`, `D564HUVM4V57ZKGG` | **$0.010/page** |
| Nova Pro standard on-demand input, `QBCT448GETVN4GKS` | **$0.000920/1,000 tokens** ($0.92/million) |
| Nova Pro standard on-demand output, `4VSJ93BQJVZVSFK5` | **$0.003680/1,000 tokens** ($3.68/million) |
| Textract async text detection, `HV3WZRSJSH5TPZZ6`, first million pages/month | **$0.001500/page**; subsequent tier $0.000600 |
| Titan Text Embeddings V2, `3WCTR7VK48VUCXDZ` | **$0.000026004/1,000 input tokens** |
| S3 Standard PUT/COPY/POST/LIST; GET/other requests | **$0.005/1,000**; **$0.0004/1,000** |
| S3 Standard storage, first 50 TB/month | **$0.023/GB-month** |
| S3 Vectors PUT data; storage | **$0.20/GB**; **$0.06/GB-month** |
| S3 Vectors request tiers 1/2, `2FSAMQMGSMVVTR7J` / `BH8GQCR5262E9BXW` | **$0.000055/request** each; no free-vector-read assumption |
| S3 Vectors QueryVectors request | **$2.50/million**, plus processed/returned-byte dimensions; not used in this study |

Public context: [Bedrock pricing](https://aws.amazon.com/bedrock/pricing/),
[Textract pricing](https://aws.amazon.com/textract/pricing/),
[S3 pricing](https://aws.amazon.com/s3/pricing/). The region catalog, rather than a
US example, supplies the numbers above. BDA custom extraction ($0.04/page in the
catalog) is not the standard-output KB parser and is not used in this estimate.

**Which pages are billed:** both paid KB parsers process **all PDFs in their
source**, including purely digital PDFs. There is no documented “only OCR when
the text layer is empty” selection for this source. Thus use 44 B pages, not just
12. An existing unchanged source normally syncs incrementally; replacing it
requires budgeting a full first ingestion, including A, rather than assuming its
previous embeddings/parser work will be reused.
[Parser billing scope](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-advanced-parsing.html)
and [sync behavior](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-data-source-sync-ingest.html).

### Comparable estimates

FM token use has not been measured. For a transparent typical scenario use **2,900
input + 750 output tokens per page**. AWS's pricing example discusses these token
figures for mixed layouts, but is ambiguously worded; applying them per page is
our conservative estimating assumption, not an AWS guarantee or this corpus's
measured token count. Planning upper scenario: **12,000 input + 4,000 output
tokens/page**, also an assumption, not a hard bound. At Nova Pro rates:

`page cost = input_tokens × 0.000000920 + output_tokens × 0.000003680`

| Scope / parser | BDA, exact page arithmetic | Nova Pro typical estimate | Nova Pro planning upper estimate |
|---|---:|---:|---:|
| Full 45-page manifest, if all were ingested | $0.45 | $0.244260 | $1.159200 |
| Actual B, 44 valid pages | $0.44 | $0.238832 | $1.133440 |
| A, **P_A pages** across five documents | $0.01 × P_A | $0.005428 × P_A | $0.025760 × P_A |
| A illustration: five one-page documents | $0.05 | $0.027140 | $0.128800 |
| A illustration: five ten-page documents | $0.50 | $0.271400 | $1.288000 |
| B + A typical illustration (49 pages) | $0.49 | $0.265972 | $1.262240 |
| B + A larger illustration (94 pages) | $0.94 | $0.510232 | $2.421440 |

**A's page count is unverified.** Five and fifty pages are explicit planning
scenarios, not observations or maximums. Obtain its page counts before approving
a precise migration budget. The five source identities/statuses were established
without reading their PDF bodies; this study did not pay to download them. If a
full second ingestion is authorized after failure, conservatively double the
relevant parser amount ($0.98/$1.88 BDA, or up to $4.842880 for the larger FM
scenario). No automatic retry or infinite “upper bound” is implied.

For a new document of `p` pages: BDA adds **$0.01p**, even for digital content;
Nova adds estimated **$0.005428p / $0.025760p**. Unchanged PDFs need not be rebilled
on every sync, but changed content is re-parsed/re-embedded; metadata-only sync
has an optimization with documented conditions. Do not churn object timestamps
or sidecars without a reason. Failure and provider-internal retry billing are
not verified; no claim is made that a rejected or ambiguous attempt is free.

### IDP already acquired the text

The current IDP pipeline calls asynchronous Textract on the **whole PDF** when
page coverage needs OCR, not just the empty pages. For B, the six scanned
one-page PDFs plus three two-page mixed PDFs plus the seven-page lease total
**19 OCR-processed PDF pages across ten documents**, although only 12 pages lack
text layers. At the Irish rate this is **$0.0285** of text-detection usage for one
successful processing of each. A hypothetical scanned-page-only path would be
$0.018; that is not the current IDP implementation. No IDP model rerun is needed
just to reuse literal page text. Historical $0.0163/document IDP averages include
other stages and are not a replacement for this OCR arithmetic.

This is an **inferred marginal usage cost, not a verified paid invoice**. The
owner reports OCR has read the documents; this study did not fetch every IDP run,
page artifact or invoice, and cannot certify that all ten durable page artifacts
exist. Code in `idp/pipeline.py` writes immutable `pages` artifacts with document
hash and numbered page text after successful processing; failed earlier stages
can prevent that artifact from being committed. `idp/ocr.py` checks full page
coverage. Prefer these saved artifacts. If missing, an existing successful
Textract job's results are retrievable for **seven days**, without starting OCR
again; verify the job/hash/coverage first. After expiry or a lost result, fresh
OCR needs new approval and costs $0.0015 per whole processed page.
[Textract result retention](https://docs.aws.amazon.com/textract/latest/dg/api-async.html).

Derived-page typical incremental OCR is therefore **$0**, or **$0.0285** to
reacquire all ten B OCR documents once if none can be reused; a single authorized
repeat of that reacquisition would be **$0.057** total. A adds **$0.0015 × R_A**
only if its `R_A` OCR-processed pages cannot be reused. If A is entirely rebuilt
into a new KB, its five documents also need embedding again. The recommended
overlay keeps A's existing digital vectors; only missing scanned pages need new
embeddings. This is a deliberate difference from full source replacement.

Shared usage remains nonzero: Titan costs **$0.000026004 per 1,000 embedded
tokens**. For example, 100,000 / 500,000 embedding tokens cost **$0.0026004 /
$0.013002**. A 1 MiB vector footprint costs about **$0.000059/month** at the stated
rate, and 1 MiB of source text about **$0.000022/month**. Twelve page objects and
twelve sidecars have 24 PUTs ($0.000120) before service reads/listing; a small-file
write may incur minimum billed sizes. These are illustrations, not measured
footprints. Allow roughly **$0.01 typical / $0.03 planning upper** for small-corpus
index/request/processing usage beyond the parser, excluding ongoing storage,
repeated retries and the separately budgeted golden run. The allowance is not an
invoice or cap. BDA/FM still pay existing IDP costs as well unless a separately
approved product redesign removes duplication.

## Evidence quality and boundaries

| Concern | BDA | FM parser | Derived text | Accept |
|---|---|---|---|---|
| Exact wording | OCR/layout may misread; standard output can include generative fields | Model-generated transcription can omit, normalize, summarize or obey embedded instructions; prompt cannot guarantee literal fidelity | Reuse OCR text, never classifier conclusions, accepted fields or summaries as purported source. OCR/NFKC normalization still differs from original glyphs | Digital text only; absence from search is not absence from the document |
| Digital baseline | Reprocessed and potentially reordered | Reprocessed through a generative stage | Keep canonical digital vectors and add only missing scanned-page text; rankings can still change as evidence pool grows | Preserves current parser/corpus |
| Chunking | Same 800/15 setting can yield different chunks due to different parsed layout | Same risk, plus model output variability | One text object per missing physical page, then existing 800/15 chunking within that page; sourcePage is identical across its chunks | Missing scanned-page chunks remain a limit |
| Citation identity | Retain original sidecars and canonical document IDs; validate actual retrieved pages | Same, plus transcription verification | Explicit trusted mapping to original document/hash/page is mandatory; returned S3 URI otherwise names the derived text | Current guarantees only; no invented page |
| Failure exposure | Fewer no-text errors expected; fallback can hide partial coverage | Same, plus model availability/throttling/content failures | Adds artifact publication and mapping failures; can isolate per-page coverage but not declare success early | Keeps known scan failures |

The metadata boundary is unchanged in principle: server-derived tenant/matter
filter **before retrieval**, then post-retrieval checks of scope/document,
selected-document constraint, live Document status INDEXED, clean malware result
and HEAD of the authorized original. See `backend/src/legaldesk/retrieval.py`.
The KB execution role already only reads supported originals and sidecars under
the synthetic tenant, invokes Titan, and manages the exact vector index. A parser
does not replace authorization, malware gating or original-object existence.

Sidecars must be named `original.pdf.metadata.json`, or e.g.
`page-0006.txt.metadata.json` for a derived page, beside the matching object.
Preserve server-owned `tenantId`, `matterId`, `documentId` and allowed attributes;
do not ingest IDP artifact JSON as legal source or trust browser-supplied scope.
Sidecars have a 10 KB limit, and S3 Vectors KB integration allows only 1 KB custom
metadata / 35 keys per vector; compact provenance should point to a trusted
server-side record instead of copying full text/history into metadata.
[S3 connector metadata](https://docs.aws.amazon.com/bedrock/latest/userguide/s3-data-source-connector.html)
and [S3 Vectors KB limits](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-bedrock-kb.html).

AWS owns `x-amz-bedrock-kb-*`, including source URI, data-source ID and page
number. **Do not forge these in a sidecar** to make a TXT file look like page 6
of a PDF. Use an application-owned `sourcePage` and a verified manifest, then
project the original page into the application's Citation. Currently the code
only reads `x-amz-bedrock-kb-document-page-number`; it does not implement that
derived-page translation. Its positive-integer parser also currently accepts
integers/digit strings, not arbitrary numeric shapes; verify the real response.
[Reserved fields/page-number support](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-test-config.html)
and [service source metadata](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-multimodal-test-and-query.html).

Crucially, AWS's page-number documentation names **OpenSearch Serverless and
Aurora**, not this S3 Vectors setup. Automatic PDF page metadata here is
**unverified**, even if scans become searchable. A paid parser is not evidence
that the frozen document/page checks will pass. TXT has no physical PDF page;
concatenating all pages into one TXT loses citation granularity. NONE chunking
also loses service page numbers and is not a solution by itself.

Advanced/markdown parsing can return a whole page/table in place of a chunk and
deduplicate results, affecting top-5 recall and context limits despite unchanged
chunk settings. The current index already declares AMAZON_BEDROCK_TEXT and
AMAZON_BEDROCK_METADATA nonfilterable. Recheck metadata/context size before
assuming tables fit.
[Chunking and table/page context](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-chunking.html).

BDA standard output has text, layout and optional generated summaries/figure
captions. Do not promote a generated caption/summary into literal legal evidence.
A custom FM parsing prompt should ask for verbatim extraction, page preservation
and explicit unreadable text, but cannot prove that no wording changed. All OCR
and parsed content remains **untrusted document data**, including injections that
become newly searchable. It must never set permissions, select tools, alter the
system prompt or auto-accept IDP facts. Keep injection/cross-matter cases and
grounding gates unchanged; inspect source spans against originals in a bounded
approved canary.
[BDA document output](https://docs.aws.amazon.com/bedrock/latest/userguide/bda-output-documents.html).

## Failure, latency, shared ingestion and F-32

The measured default ingestion was 8.047 s; no BDA, FM or derived-source latency
was measured. For operator planning only, allow **1–5 minutes typical / 15 minutes
contingency** for a small advanced-parser ingestion; not an SLA. FM input/output
tokens, quota/throttling and cross-Region capacity affect this. Reusing artifacts
avoids fresh asynchronous OCR latency; missing artifacts add OCR completion and
publication latency. Accepting the limit leaves current ingestion behavior.

Budget one initial ingestion and, only after a recorded diagnosis and fresh
authorization, at most one retry. Preserve job IDs, stats, per-document outcomes
and immutable checkpoints. Managed parser failure can fall back to the default
parser, which still cannot recover an image-only document and can leave a mixed
PDF only partly searchable. No parser success flag proves full page coverage.
[Parsing fallback contract](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_ParsingConfiguration.html).

The source spans **both matters**. Serialize against the supervisor's shared-KB
lease/operating window, uploads, reconciliation and cleanup; do not start a
second migration job while an ambiguous operation exists. This checkout's
`state.py` explicitly locks the selected document set, while Bedrock sync is
source-wide; that is not proof of a general cross-matter lease. The live lease
record was not read. Confirm the current shared lease mechanism with the
supervisor before any implementation execution. The operation TTL is two hours;
expiry is not evidence that an AWS job stopped. Do not release/overwrite a lock
just because a call timed out. Separate sources/KBs also need coordinated
cutover, rather than concurrent production syncs assumed safe.

**F-32 is verified independently:** the live application role's inline policy
has Retrieve, StartIngestionJob, GetIngestionJob, ApplyGuardrail and InvokeModel,
but no GetKnowledgeBaseDocuments and no attached policies. At this commit,
`_resolve_document_statuses` catches every client error and returns None; COMPLETE
with any source-wide failure keeps the operation pending when resolution cannot
be proven. The study did not invoke the application to reproduce it or fix IAM.

- **BDA/FM:** expected to reduce no-text failures (six B, potentially A's seventh
  scanned source); new parser failures are also possible. The truncated B PDF
  remains invalid. **As long as any failure remains in the shared source, F-32's
  branch still runs**, including operations selecting otherwise good documents.
- **Derived overlay:** keeps failing scanned originals in the original source
  unless a separately designed source/status migration excludes them. Therefore
  it does **not** remove F-32 from current original-source operations. A clean
  derived source may see fewer failures, but needs its own status mapping; do
  not declare canonical originals indexed from the wrong provider identifier.
- **Accept:** preserves the current failure frequency and the open-operation
  behavior. Explaining the limitation does not repair lifecycle reporting.

Reducing failures is not a substitute for the separately scoped permission/error
handling correction. Conversely, F-32's fix will make outcomes terminal but will
not add OCR or make missing evidence searchable.

## Deployment, exact blast radius and rollback — proposed, not executed

### Replacement facts

CloudFormation's ParsingConfiguration property explicitly requires **Replacement**.
The live Irish `DescribeType(AWS::Bedrock::DataSource)` schema confirms
`/properties/VectorIngestionConfiguration/ParsingConfiguration` is createOnly.
The user guide allows some same-strategy API configuration updates; that does
not authorize an in-place default→BDA/FM switch, nor override CFN replacement.
[VectorIngestionConfiguration](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-bedrock-datasource-vectoringestionconfiguration.html)
and [parser-change restrictions](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-data-source-customize-ingestion.html).

The documented multimodal configuration requires an S3 supplemental destination
at KB creation and says it cannot be added later. The live KB lacks it; the
Irish KB schema marks VectorKnowledgeBaseConfiguration createOnly, and CFN marks
SupplementalDataStorageConfiguration Replacement. Therefore the safe documented
multimodal BDA/FM path is a **new KB, new source and dedicated vector index**, not
just replacing XIA81RBIPG in place. A legacy text-only parser with omitted
ParsingModality might avoid image-storage needs, but this combination on the
existing KB was **not verified** and is not the migration assumption.
[KB supplemental configuration](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-bedrock-knowledgebase-vectorknowledgebaseconfiguration.html).

Live stack/Lambda inventory found these consumers. The old lowercase stack name
`legaldesk-phase-03` in historical instructions is **not** the deployed KB stack.

| Live stack | Reference and deployment implication |
|---|---|
| **LegalDeskPhase03KnowledgeBase** | Owns KB, source, execution role, vector bucket/index. Source output XIA81RBIPG; KB output AQWCFL7239 |
| **LegalDeskPhase14PublicEdge** | Parameters KnowledgeBaseId and DataSourceId. `LegalDeskPhase14PublicEdge-application` and `LegalDeskPhase14PublicEdge-agent-chat-worker` each have both LEGALDESK_KNOWLEDGE_BASE_ID and LEGALDESK_DATA_SOURCE_ID |
| **LegalDeskPhase14Reconciliation** | Same two parameters/env values on `LegalDeskPhase14Reconciliation-reconciliation`; status/deletion/integrity operations must target the right source |
| **LegalDeskPhase08Gateway** | KnowledgeBaseId parameter, scoped access, and `LegalDeskSearchDocumentsPhase15` with LEGALDESK_KNOWLEDGE_BASE_ID only. **No live DataSourceId env reference found**; source-only change does not require inventing one |
| **LegalDeskPhase14IDP** | No KB/source ID parameter/env match found; owns OCR/artifact pipeline. Derived-text publication needs explicit integration here, not raw access from the browser |
| **LegalDeskProductionCD**, **LegalDeskPhase15EdgeBootstrap** | Deployment/control roles, not discovered KB/source consumers. CD is code-only; inspect exact approved change-set/temporary-role path rather than assuming it can change IAM/parameters |

All regional stack descriptions and Lambda configurations were inspected for the
two exact ID values; no other Lambda env match was found. This does not discover
untracked operator files, another account/Region or a runtime cache. No separate
dev KB using these IDs was found. Local eval environments and copied runbooks
must be checked at cutover.

### BDA/FM migration sequence

1. Obtain owner approval for parser choice, EU processing, exact budget including
   A's verified pages, canary, migration and rollback. Re-read all live state and
   resolve/document the BDA availability conflict. Reconcile existing open
   operations and F-32 separately; preserve original IDs/hashes and statuses.
2. Prepare an additive sibling KB stack (proposed name
   `LegalDeskPhase15OcrKnowledgeBase`) with unique physical KB/role names, a
   dedicated vector bucket/index, a private SSE-S3 supplemental prefix outside
   the source inclusion path, the selected parser and the existing 800/15/Titan
   settings. No edit is supplied here. Review its exact change set/IAM; leave
   LegalDeskPhase03KnowledgeBase intact. Avoid two KBs writing the same index.
3. Reuse the **existing clean S3 originals and sidecars**; no repeat upload,
   quarantine scanning, IDP classification/extraction or fresh Textract is needed
   merely to parse them in the new KB. The source includes A and B: budget and
   verify all five A documents as well. New source identity requires new vectors;
   no promise of parser/embedding-cache reuse. Keep the deliberately invalid file
   a measured failure; do not silently remove it to get a green report.
4. In the approved shared window, ingest once; verify each expected document and
   all relevant pages, including lease page 6, actual citation metadata and
   cross-matter exclusion. Read-only status checks alone are insufficient for
   text/page acceptance. Any model/retrieval canary requires its own allowance.
5. Only after validation, update PublicEdge and Reconciliation's **both** IDs and
   Gateway's KB ID plus exact KB-scoped IAM resources using reviewed change sets.
   Follow the existing temporary-role/code-only CD procedure and closure; do not
   broaden CD or leave bootstrap permissions behind. Drain/expire old workers and
   operations before switch. All consumers must use one consistent generation.
6. Record new IDs/configuration with the eval release/artifact, then run the
   separately approved frozen golden set. Retain the old KB until rollback is
   explicitly released. No automatic teardown.

Both matters remain on the old KB until switch. Their Document IDs, original
objects, IDP facts/review history and upload quota usage remain the same; new
indexing statuses need deliberate reconciliation. Never fake INDEXED to pass
retrieval's fail-closed checks. During a direct in-place source replacement,
DELETE can remove old vectors and create a search gap for **both** matters; a
rollback replacement does not restore XIA81RBIPG or deleted vectors. That is why
the additive migration is preferred.

Rollback of the additive path: stop new migration activity, restore the old ID
parameters/env and exact old IAM bindings across the three consumer stacks,
redeploy matching code/configuration and recheck A/B retrieval/status. Preserve
failed-generation evidence. Only later, with separate cleanup approval, delete
the new source/KB/index/supplemental artifacts. Keeping old source vectors avoids
repaying their parsing just to roll back. A new KB does not itself copy or mutate
IDP records.

### Recommended derived-page sequence

1. Approve an architecture/retention extension: original PDF remains canonical;
   derived text is literal evidence with source hash/page provenance, not IDP
   conclusions. Verify each saved pages artifact and its hash/full-page coverage;
   recover retained Textract results only where necessary and authorized.
2. Propose `rag-derived/v1/tnt_phase14_20260928/<matter>/<document>/page-NNNN.txt`
   in the existing document bucket, **outside** `tenants/` so the existing source
   does not crawl it. Add one default-parser source in LegalDeskPhase03KnowledgeBase
   with that exact synthetic-tenant prefix and existing 800/15 chunking. Publish
   only missing scanned pages, leaving digital originals/vectors unchanged.
3. Implement publication idempotency by original hash/page/version, sidecars,
   trusted source mapping, page-aware Citation translation, and per-logical-document
   status aggregation. Each original scan's “indexed” state must be supported by
   all required derived pages. Six NOT_FOUND originals do not become indexed
   simply because a second source has a similar file. Ingestion/status APIs,
   reconciliation and governance currently assume the canonical source URI and
   single source ID; extend them explicitly.
4. Keep original clean-object HEAD/authorization checks, selected-document
   constraints and IDP semantics. Do not expose derived objects as new user
   documents or pollute inventory counts/order. Delete/update derivatives and
   vectors when originals change or are deleted, including stale generations.
   Publish text then its validated sidecar before starting the serialized sync;
   incomplete pairs remain unusable, not partially trusted evidence.
5. Deploy reviewed changes to KB role/source, IDP publisher, PublicEdge app/worker,
   Gateway search code and Reconciliation. Add tests for forged/missing mapping,
   incomplete OCR coverage, wrong hash/page, duplicate publication, delete/update,
   cross-matter retrieval and real original-page citation opening. Then approve
   a small end-to-end paid canary before the golden run. This is future work.

This adds a source without replacing XIA81RBIPG/AQWCFL7239. Original digital
vectors need no rebuild; new scanned-page vectors do. A's missing scan gets the
same treatment after verifying its page coverage. Search pool/rankings change,
so even unchanged digital cases need a fresh comparable report. Roll back by
disabling derived-source use in the trusted adapter and restoring prior code/
logical statuses; under separate approval, remove derived-source vectors and
objects. Do not delete originals or IDP artifacts. Native PDFs with newly added
OCR text layers are another possibility, but overwriting canonical originals
would change hashes and invalidate IDP evidence; shadow PDFs still require
mapping and do not solve unverified S3 Vectors page metadata automatically.

### Exact KB-role IAM deltas to review

Current role: `arn:aws:iam::344774635844:role/LegalDeskKnowledgeBasePhase03`.
Its live inline policy matches the original/sidecar, Titan and exact-index scopes
described above. **No role was modified.** A sibling KB gets a separate role with
the same baseline scopes and its own index ARN. Existing trust conditions must
remain account/KB-scoped; no wildcard service action, S3 bucket expansion,
Marketplace subscription or unrelated model access is needed.

For **Nova Pro**, add only `bedrock:InvokeModel` on these five resources:

```text
arn:aws:bedrock:eu-west-1:344774635844:inference-profile/eu.amazon.nova-pro-v1:0
arn:aws:bedrock:eu-west-1::foundation-model/amazon.nova-pro-v1:0
arn:aws:bedrock:eu-west-3::foundation-model/amazon.nova-pro-v1:0
arn:aws:bedrock:eu-central-1::foundation-model/amazon.nova-pro-v1:0
arn:aws:bedrock:eu-north-1::foundation-model/amazon.nova-pro-v1:0
```

Constrain the foundation-model grants with `bedrock:InferenceProfileArn` equal
to that exact profile if using the profile; do not add InvokeModelWithResponseStream
or InvokeModel* by habit. Confirm current destinations again before approval.
[Cross-Region IAM](https://docs.aws.amazon.com/bedrock/latest/userguide/geographic-cross-region-inference.html).

For **BDA**, the required actions are `bedrock:InvokeDataAutomationAsync` and
`bedrock:GetDataAutomationStatus`. The concrete proposed resource set for review:

- Invoke: `arn:aws:bedrock:eu-west-1:aws:data-automation-project/public-rag-default`.
- Invoke on these six EU profile resources, per BDA's cross-Region requirement:

```text
arn:aws:bedrock:eu-west-1:344774635844:data-automation-profile/eu.data-automation-v1
arn:aws:bedrock:eu-west-3:344774635844:data-automation-profile/eu.data-automation-v1
arn:aws:bedrock:eu-central-1:344774635844:data-automation-profile/eu.data-automation-v1
arn:aws:bedrock:eu-north-1:344774635844:data-automation-profile/eu.data-automation-v1
arn:aws:bedrock:eu-south-1:344774635844:data-automation-profile/eu.data-automation-v1
arn:aws:bedrock:eu-south-2:344774635844:data-automation-profile/eu.data-automation-v1
```

- Status: `arn:aws:bedrock:eu-west-1:344774635844:data-automation-invocation/*`.

The KB-permissions page's BDA example uses **empty account segments** for profile
and invocation, whereas BDA's runtime API/profile guide require account-qualified
ARNs. The proposed account-qualified set follows that API/CRIS evidence, **but
the exact KB-integration authorization path is unverified**. Resolve this in the
reviewed deployment/canary; do not grant both shapes or `Resource:*` to guess.
If the managed KB path needs a different service-owned profile, stop and obtain
its exact documented ARN. Do not add six-region project/status wildcards on
speculation. [KB permissions](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-permissions.html),
[BDA runtime ARN patterns](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_data-automation-runtime_InvokeDataAutomationAsync.html).

For a **multimodal supplemental destination**, propose the existing document
bucket prefix `kb-supplemental/phase15-ocr/` outside ingestion. Add `s3:ListBucket`
on `arn:aws:s3:::legaldeskphase02documents-documentbucket-ojlu4kvhlied`, constrained
to that prefix, and `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` only on
`arn:aws:s3:::legaldeskphase02documents-documentbucket-ojlu4kvhlied/kb-supplemental/phase15-ocr/*`,
with `aws:ResourceAccount=344774635844`. This is a proposed exact location, not an
existing verified destination. Use SSE-S3; **no new KMS permissions** are proposed.
If a CMK is chosen later, list/review its exact key/actions separately.

For **derived text**, add only `s3:GetObject` on the page TXT and matching sidecar
patterns beneath each of these **two exact** matter prefixes:

```text
arn:aws:s3:::legaldeskphase02documents-documentbucket-ojlu4kvhlied/rag-derived/v1/tnt_phase14_20260928/mat_phase14_a_20260928/*/page-*.txt
arn:aws:s3:::legaldeskphase02documents-documentbucket-ojlu4kvhlied/rag-derived/v1/tnt_phase14_20260928/mat_phase14_a_20260928/*/page-*.txt.metadata.json
arn:aws:s3:::legaldeskphase02documents-documentbucket-ojlu4kvhlied/rag-derived/v1/tnt_phase14_20260928/mat_phase14_b_20260928/*/page-*.txt
arn:aws:s3:::legaldeskphase02documents-documentbucket-ojlu4kvhlied/rag-derived/v1/tnt_phase14_20260928/mat_phase14_b_20260928/*/page-*.txt.metadata.json
```

Add ListBucket prefix conditions for those two exact derived matter paths; keep
the original prefix permission. The KB role needs **no Textract**, IDP-artifact,
classifier/model, S3 write or supplemental-storage permission for plain TXT.
Publisher permissions belong on a separately reviewed IDP/publication role, not
on the KB role. Accepting the limit needs **no IAM addition**. F-32's application
GetKnowledgeBaseDocuments grant is unrelated to these KB-role deltas and is not
implemented in this PR.

## Frozen golden scoring and operator references

The no-network golden preflight returns dataset hash
`c88682195c3806db0210c5b3b47253f1e577d5d1425201aaa05bce42f80ada07`
and thresholds hash
`70316c905b431aab3306d065771f57e2ea580630500cdeefa3ee43fdf8e64f99`.
There are 80 cases (47 dev / 33 test). Counts below came from the frozen JSON,
not from a real evaluation. No dataset/threshold change was made.

All **25** cases mentioning the six scans, including expected inventory lists:
`lt-03`, `lt-04`, `lt-10`, `lt-12`, `lt-21`, `id-01`, `id-02`, `id-03`, `id-04`,
`id-05`, `id-06`, `id-07`, `id-09`, `id-10`, `id-11`, `id-12`, `id-14`,
`inv-01`, `inv-02`, `inv-04`, `inv-05`, `inv-06`, `mx-02`, `un-05`, `am-05`.
This is exposure, **not a claim of 25 unavoidable failures**. IDP answers may
work independently; unfiltered inventory may still contain a failed document.
The indexed-only inventories `inv-02` and `inv-05` disagree with six missing
INDEXED records once honest terminal statuses are reflected.

| Option | Expected scoring interpretation, with unchanged oracle |
|---|---|
| BDA | Can remove the missing-text cause for the eight scan-linked RAG cases and mixed-page `lt-19`; actual OCR/recall/pages/citations must still pass. Digital cases need remeasurement because all PDFs were re-parsed. Fallback/partial parsing is measured as failure, not excused |
| FM | Same opportunity, with extra transcription variability. A plausible paraphrase of a clause is not proof of original wording/page. No pass prediction is justified without the real run |
| Derived text | Can recover missing scanned-page evidence while keeping digital parsing. Only works if returned logical original document IDs/pages match the frozen expectations. Bare TXT with null page or derived object IDs fails retrieval/citation scoring |
| Accept | Preserve real failures and state the limitation in product/report. Do not relabel scans as expected FAILED, waive page checks or lower thresholds |

The eight RAG-expectation cases are `lt-03`, `lt-04`, `lt-10`, `lt-12`, `lt-21`,
`mx-02`, `un-05`, `am-05`. **Seven** have missing scanned gold document/page
occurrences; `un-05` expects insufficient_evidence with **empty gold**, so its
successful abstention is possible despite a missing scan. `am-05` requires a
three-document comparison including one missing scanned judgment. `lt-19` needs
the lease's scanned page 6 and has a known-limit hypothesis, not a proven miss.

There are **43** gold occurrences across all cases, of which **7** refer to the
wholly scanned missing PDFs. Even granting perfect retrieval of everything else,
overall recall is at most **36/43 = 0.8372**. The actual **test split** has 16 gold
occurrences, **2 missing**, giving an optimistic ceiling of **14/16 = 0.875**,
below the frozen **0.90 retrievalRecallAt5** gate. These are arithmetic ceilings
conditional on those documents remaining absent, not measured scores. Lease page
6 would lower overall recall to **35/43 = 0.8140** if its absence were confirmed;
that case is dev, so it does not lower the test-split ceiling. Exact-page metadata
failures could lower recall further. Known-limit cases remain in stage metrics;
their special answerAccuracy treatment must not hide retrieval failures.

F-32 can cause an **earlier** lifecycle/status error or aborted run, so actual
firstFailure need not be retrieval. The frozen run probes RAG and separately
measures final behavior: do not claim a retrieval score from IDP fields or from
an offline simulation. A full real run remains separately authorized and costs
far more than these small-corpus parser figures.

If the source ID changes, update **PublicEdge/Reconciliation stack parameters and
Lambda env**, local eval `LEGALDESK_DATA_SOURCE_ID`, any operator status/sync/
cleanup commands, and persisted operation-generation handling. If KB ID changes,
also update Gateway/search env and exact role resource scopes, local eval
`LEGALDESK_KNOWLEDGE_BASE_ID`, and all affected KB references. Relevant files:
`docs/phase-15-corpus-operator.md`, `docs/phase-14-data-governance-runbook.md`,
`docs/phase-15-live-runbook.md`, `docs/phase-15-golden-set.md`, and `docs/rag.md`.
The golden CLI has no data-source-ID flag: its local composition reads the
environment, while asynchronous cases use deployed workers. Update **both**.
The `{goldenId: realDocumentId}` document map stays the same if originals are
reused; no replacement data-source ID belongs in that map. Record new source/KB,
parser/model configuration and generation alongside release/artifact hashes in
operator evidence, without changing frozen dataset or thresholds.

For derived sources, record **both** source IDs and their purpose rather than
overwriting the original ID everywhere; ingestion/status/governance must route
deterministically. Prior reports remain immutable. Existing routing/holdout
measurements describe the old parsing/corpus generation and cannot be presented
as validation of a newly parsed corpus.

## Verification limits and handoff

Verified live: expected account/login identity; KB/source/ingestion-job state;
explicit per-document B statuses and A's five-source outcome; Irish CFN schemas;
KB-role policy; application-role missing permission; all regional stack and Lambda
ID references; BDA public RAG project; Nova profile and availability; Claude
3.5/3.7 availability limitations. Verified locally: all manifest hashes and
45/12 versus uploaded 44/12 counts, ten whole-document OCR candidates/19 pages,
golden case/gold/split arithmetic and frozen no-network preflight.

Unverified: mixed-page vectors/text coverage, actual returned page metadata in
S3 Vectors, A's page counts, every durable OCR artifact, actual per-corpus paid
bill, advanced parser fidelity/latency/failure billing, BDA KB integration's
regional/IAM ambiguities, SCP effects, and all migration/rollback executions.
Those gaps are explicit gates for future work, not guessed successes.

Validation for this documentation-only change: local corpus/hash/page and dataset
arithmetic scripts, no-network golden preflight, Markdown link/structure and
`git diff --check`. No application/IaC tests are needed for unchanged code/templates.
No AWS resources were created or modified. Shared-vault/failure-register updates
belong to the supervisor; no new architecture decision has been adopted here.
