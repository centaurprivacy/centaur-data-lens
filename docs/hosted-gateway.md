# Future Centaur-hosted adapter contract

Hosted inference is not implemented in this repository.

If introduced later, it must remain optional and use a separate service with:

- Optional Centaur authentication required only for Centaur-funded inference
- A quote endpoint returning a stable analysis-credit cost and hard payload cap
- Short-lived job authorization separate from account and billing records
- Idempotent job creation and automatic refunds on failed jobs
- Streaming responses with structured citations
- No archive upload and no prompt/response persistence by default
- Billing records containing opaque job IDs and usage units, not platform,
  archive, question, prompt, or report content
- A verified retention/ZDR arrangement disclosed before confirmation

The CLI adapter must show the destination, model/provider, categories, record
count, byte count, exclusions, and credit cost before transmitting.
