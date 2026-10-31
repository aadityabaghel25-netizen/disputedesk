# PayPal dispute evidence guide (DisputeDesk knowledge base)

> Plain-language summary written for DisputeDesk, based on PayPal's public Seller Protection
> policy and dispute documentation. It is guidance for drafting replies, not legal advice.
> Before the final demo, re-check each section against the current PayPal pages:
> https://www.paypal.com/us/legalhub/paypal/useragreement-full (Seller Protection Program)
> https://developer.paypal.com/docs/api/customer-disputes/v1/

## MERCHANDISE_OR_SERVICE_NOT_RECEIVED
Buyer says the item never arrived.
- Strongest evidence: online tracking that shows the item was **delivered**, to the **shipping address on the PayPal transaction**, with the delivery date.
- For higher-value orders, signature confirmation of delivery greatly strengthens the case (PayPal Seller Protection in the US asks for signature confirmation on items of $750 or more).
- Shipping date: show the item was shipped on time.
- Buyer messages that acknowledge receipt are very strong.
- Evidence type to submit: PROOF_OF_FULFILLMENT with carrier name and tracking number.
- Weak case: no tracking, tracking without a delivered status, or delivery to a different address.

## MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED
Buyer says the item is significantly different from the listing.
- Strongest evidence: the original listing description and photos, photos of the actual item before shipping, and messages showing what the buyer was told.
- PayPal often asks the buyer to return the item; proof the return was or wasn't received matters.
- Minor differences (small colour shade differences, normal wear stated in the listing) are generally not "significantly not as described".
- Evidence type to submit: OTHER (with listing details and photos in notes/attachments); PROOF_OF_FULFILLMENT for shipping.
- Weak case: the listing did not mention the difference the buyer complains about.

## UNAUTHORISED
Buyer says they did not make this payment.
- Strongest evidence: delivery to the address on the transaction, signature confirmation, prior purchases by the same buyer to the same address, and buyer messages before or after the sale.
- Digital goods and services are harder to defend.
- Evidence type to submit: PROOF_OF_FULFILLMENT, OTHER.

## CREDIT_NOT_PROCESSED
Buyer says a refund was promised but not received.
- Strongest evidence: proof the refund was issued (refund transaction id), or proof the return was never received or the refund was never promised.
- Evidence type to submit: PROOF_OF_REFUND or OTHER.

## DUPLICATE_TRANSACTION
Buyer says they were charged twice.
- Strongest evidence: proof that both payments were for separate orders, or proof one was already refunded.
- Evidence type to submit: OTHER or PROOF_OF_REFUND.

## INCORRECT_AMOUNT
Buyer says the amount charged was wrong.
- Strongest evidence: the order total the buyer approved at checkout, invoice, and shipping/tax breakdown.
- Evidence type to submit: OTHER.

## OTHER
- Gather the order, shipping and full message history, then answer the buyer's specific complaint.

## General rules for every reply
- Answer before `seller_response_due_date`. Missing the deadline usually means losing.
- Be factual, short and polite. Every factual sentence should point to a piece of evidence.
- Do not include anything you cannot prove.
- Never share the buyer's personal data beyond what is in the transaction.
