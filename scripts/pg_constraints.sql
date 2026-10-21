\set ON_ERROR_STOP on
CREATE OR REPLACE FUNCTION expect_fail(label text, stmt text) RETURNS void AS $$
BEGIN
  BEGIN EXECUTE stmt; RAISE EXCEPTION 'NOT REJECTED: %', label;
  EXCEPTION WHEN unique_violation OR check_violation OR foreign_key_violation OR not_null_violation THEN RAISE NOTICE 'ok (rejected): %', label; END;
END $$ LANGUAGE plpgsql;
CREATE OR REPLACE FUNCTION expect_ok(label text, stmt text) RETURNS void AS $$
BEGIN EXECUTE stmt; RAISE NOTICE 'ok (accepted): %', label; END $$ LANGUAGE plpgsql;

INSERT INTO invoices VALUES ('I1','INV-0001','INV1',5000,'UNPAID',now(),NULL),('I2','INV-0002','INV2',3000,'UNPAID',now(),NULL);
INSERT INTO provider_transactions (provider_txn_id,provider,amount,status,occurred_at,source) VALUES
  ('R1','mpesa_daraja',5000,'SUCCESS',now(),'t'),('R2','mpesa_daraja',3000,'SUCCESS',now(),'t');
SELECT expect_ok('apply R1 to I1', $$INSERT INTO applied_keys (idempotency_key,invoice_id) VALUES ('k1','I1')$$);
SELECT expect_ok('record R1', $$INSERT INTO payment_applications (provider_txn_id,idempotency_key,invoice_id,amount) VALUES ('R1','k1','I1',5000)$$);
SELECT expect_fail('same key claimed twice', $$INSERT INTO applied_keys (idempotency_key,invoice_id) VALUES ('k1','I1')$$);
SELECT expect_ok('second key for I2', $$INSERT INTO applied_keys (idempotency_key,invoice_id) VALUES ('k2','I2')$$);
SELECT expect_fail('R1 reused for another invoice under another key', $$INSERT INTO payment_applications (provider_txn_id,idempotency_key,invoice_id,amount) VALUES ('R1','k2','I2',3000)$$);
SELECT expect_fail('payment that is not in the ledger', $$INSERT INTO payment_applications (provider_txn_id,idempotency_key,invoice_id,amount) VALUES ('NOPE','k2','I2',3000)$$);
SELECT expect_fail('application under a key that was never claimed', $$INSERT INTO payment_applications (provider_txn_id,idempotency_key,invoice_id,amount) VALUES ('R2','nokey','I2',3000)$$);
SELECT expect_fail('zero amount applied', $$INSERT INTO payment_applications (provider_txn_id,idempotency_key,invoice_id,amount) VALUES ('R2','k2','I2',0)$$);
SELECT expect_ok('R2 to I2', $$INSERT INTO payment_applications (provider_txn_id,idempotency_key,invoice_id,amount) VALUES ('R2','k2','I2',3000)$$);

SELECT expect_ok('a payment can be marked REVERSED', $$UPDATE provider_transactions SET status = 'REVERSED' WHERE provider_txn_id = 'R2'$$);
SELECT expect_fail('an unknown payment status', $$UPDATE provider_transactions SET status = 'BOUNCED' WHERE provider_txn_id = 'R2'$$);
SELECT expect_ok('back to SUCCESS for the checks below', $$UPDATE provider_transactions SET status = 'SUCCESS' WHERE provider_txn_id = 'R2'$$);

SELECT expect_ok('stk request', $$INSERT INTO stk_requests VALUES ('ws_1','m1','push-1','INV-0001',1000,'enc','2547****149',NULL,now())$$);
SELECT expect_fail('same push key twice', $$INSERT INTO stk_requests VALUES ('ws_2','m2','push-1','INV-0001',1000,'enc','2547****149',NULL,now())$$);
SELECT expect_fail('same CheckoutRequestID twice', $$INSERT INTO stk_requests VALUES ('ws_1','m3','push-9','INV-0001',1000,'enc','2547****149',NULL,now())$$);
SELECT expect_fail('stk amount zero', $$INSERT INTO stk_requests VALUES ('ws_3','m3','push-3','INV-0001',0,'enc','2547****149',NULL,now())$$);
SELECT expect_fail('stk request without a sealed number', $$INSERT INTO stk_requests VALUES ('ws_4','m4','push-4','INV-0001',10,NULL,'2547****149',NULL,now())$$);

SELECT expect_ok('reversal #1 SENDING', $$INSERT INTO refund_requests (idempotency_key,provider_txn_id,amount,status) VALUES ('r1','R1',5000,'SENDING')$$);
SELECT expect_fail('second live reversal of the same payment (new key)', $$INSERT INTO refund_requests (idempotency_key,provider_txn_id,amount,status) VALUES ('r2','R1',5000,'SENDING')$$);
SELECT expect_ok('reversal #1 rejected cleanly', $$UPDATE refund_requests SET status='REJECTED' WHERE idempotency_key='r1'$$);
SELECT expect_ok('retry under a new key after a clean rejection', $$INSERT INTO refund_requests (idempotency_key,provider_txn_id,amount,status) VALUES ('r2','R1',5000,'SENDING')$$);
SELECT expect_ok('r2 accepted', $$UPDATE refund_requests SET status='ACCEPTED' WHERE idempotency_key='r2'$$);
SELECT expect_fail('third while r2 is accepted', $$INSERT INTO refund_requests (idempotency_key,provider_txn_id,amount,status) VALUES ('r3','R1',5000,'SENDING')$$);
SELECT expect_ok('r2 completes', $$UPDATE refund_requests SET status='COMPLETED' WHERE idempotency_key='r2'$$);
SELECT expect_fail('another while r2 is completed', $$INSERT INTO refund_requests (idempotency_key,provider_txn_id,amount,status) VALUES ('r4','R1',5000,'SENDING')$$);
SELECT expect_fail('invalid refund status', $$INSERT INTO refund_requests (idempotency_key,provider_txn_id,amount,status) VALUES ('r5','R2',100,'MAYBE')$$);
SELECT expect_ok('different payment is independent', $$INSERT INTO refund_requests (idempotency_key,provider_txn_id,amount,status) VALUES ('r6','R2',3000,'SENDING')$$);

SELECT expect_ok('notification', $$INSERT INTO notification_outbox (idempotency_key,invoice_id,template) VALUES ('n1','I1','payment_failed')$$);
SELECT expect_fail('duplicate notification key', $$INSERT INTO notification_outbox (idempotency_key,invoice_id,template) VALUES ('n1','I1','payment_failed')$$);
SELECT expect_fail('invalid outbox status', $$INSERT INTO notification_outbox (idempotency_key,invoice_id,template,status) VALUES ('n2','I1','t','LOST')$$);

SELECT expect_ok('inbox: a delivery is stored', $$INSERT INTO webhook_inbox (payload_sha256, kind, received_at, payload, status) VALUES ('h1','stk',now(),'{}','RECEIVED')$$);
SELECT expect_fail('inbox: the same payload hash twice (a redelivery)', $$INSERT INTO webhook_inbox (payload_sha256, kind, received_at, payload, status) VALUES ('h1','stk',now(),'{}','RECEIVED')$$);
SELECT expect_fail('inbox: unknown kind', $$INSERT INTO webhook_inbox (payload_sha256, kind, received_at, payload, status) VALUES ('h2','validation',now(),'{}','RECEIVED')$$);
SELECT expect_fail('inbox: unknown status', $$INSERT INTO webhook_inbox (payload_sha256, kind, received_at, payload, status) VALUES ('h3','stk',now(),'{}','DONE')$$);
SELECT expect_ok('inbox: a quarantined item with a reason', $$INSERT INTO webhook_inbox (payload_sha256, kind, received_at, payload, status, detail) VALUES ('h4','c2b',now(),'{}','QUARANTINED','amount differs')$$);

SELECT expect_ok('inbox: a payroll (B2C) result', $$INSERT INTO webhook_inbox (payload_sha256, kind, received_at, payload, status) VALUES ('h5','b2c-result',now(),'{}','RECEIVED')$$);
SELECT expect_ok('payroll: a draft batch', $$INSERT INTO payout_batches (batch_id,status,total,lines,created_by,created_at) VALUES ('W43','DRAFT',2700,2,'mike',now())$$);
SELECT expect_fail('payroll: approved without saying who approved', $$UPDATE payout_batches SET status='APPROVED' WHERE batch_id='W43'$$);
SELECT expect_ok('payroll: approved by a person', $$UPDATE payout_batches SET status='APPROVED', approved_by='mike', approved_at=now() WHERE batch_id='W43'$$);
SELECT expect_ok('payroll: line 2', $$INSERT INTO payouts (payout_id,batch_id,line_no,name,payee_key,msisdn_enc,msisdn_masked,amount,status,created_at,updated_at) VALUES ('W43:2','W43',2,'Grace','pk1','enc','2547****678',1500,'QUEUED',now(),now())$$);
SELECT expect_fail('payroll: the same person twice in one batch', $$INSERT INTO payouts (payout_id,batch_id,line_no,name,payee_key,msisdn_enc,msisdn_masked,amount,status,created_at,updated_at) VALUES ('W43:9','W43',9,'Grace again','pk1','enc','2547****678',1500,'QUEUED',now(),now())$$);
SELECT expect_fail('payroll: cents', $$INSERT INTO payouts (payout_id,batch_id,line_no,name,payee_key,msisdn_enc,msisdn_masked,amount,status,created_at,updated_at) VALUES ('W43:3','W43',3,'Peter','pk2','enc','2547****111',1200.50,'QUEUED',now(),now())$$);
SELECT expect_fail('payroll: a line for a batch that does not exist', $$INSERT INTO payouts (payout_id,batch_id,line_no,name,payee_key,msisdn_enc,msisdn_masked,amount,status,created_at,updated_at) VALUES ('X:2','X',2,'Peter','pk2','enc','2547****111',1200,'QUEUED',now(),now())$$);
SELECT expect_ok('payroll: line 3', $$INSERT INTO payouts (payout_id,batch_id,line_no,name,payee_key,msisdn_enc,msisdn_masked,amount,status,created_at,updated_at,originator_conversation_id) VALUES ('W43:3','W43',3,'Peter','pk2','enc','2547****111',1200,'SENDING',now(),now(),'W43:3:1')$$);
SELECT expect_fail('payroll: one request id used for two lines', $$UPDATE payouts SET originator_conversation_id='W43:3:1' WHERE payout_id='W43:2'$$);
SELECT expect_fail('payroll: PAID without a receipt', $$UPDATE payouts SET status='PAID' WHERE payout_id='W43:2'$$);
SELECT expect_ok('payroll: PAID with a receipt', $$UPDATE payouts SET status='PAID', receipt='UJKB2C0001' WHERE payout_id='W43:2'$$);
SELECT expect_fail('payroll: one receipt for two people', $$UPDATE payouts SET status='PAID', receipt='UJKB2C0001' WHERE payout_id='W43:3'$$);
SELECT expect_fail('payroll: an unknown line status', $$UPDATE payouts SET status='MAYBE' WHERE payout_id='W43:3'$$);
SELECT expect_fail('payroll: an unknown name check', $$UPDATE payouts SET name_check='PROBABLY' WHERE payout_id='W43:2'$$);
SELECT expect_fail('payroll: a name mismatch without the registered name kept', $$UPDATE payouts SET name_check='MISMATCH' WHERE payout_id='W43:2'$$);
SELECT expect_ok('payroll: a name mismatch with the sealed registered name', $$UPDATE payouts SET name_check='MISMATCH', paid_to_enc='enc1:x' WHERE payout_id='W43:2'$$);
SELECT expect_ok('payroll: a person settles it', $$UPDATE payouts SET name_check='WRONG_PERSON' WHERE payout_id='W43:2'$$);
