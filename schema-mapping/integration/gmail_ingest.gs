/**
 * MigrateHQ Gmail Ingest
 *
 * Monitors a Gmail label for emails with CSV attachments and POSTs them
 * to the /api/ingest endpoint. Designed to run on a time-driven trigger
 * every 5 minutes.
 *
 * Script Properties (set via Apps Script > Project Settings > Script Properties):
 *   MIGRATEHQ_API_BASE_URL  — e.g. https://api.migratehq.com (no trailing slash)
 *   MIGRATEHQ_API_TOKEN     — Bearer token for API auth (treat as a secret)
 *   GMAIL_LABEL             — Gmail label to monitor (default: "MigrateHQ/Ingest")
 *   TARGET_SCHEMA_URL       — HTTPS URL to the target-schema CSV file stored in
 *                             Google Drive (published as CSV or fetched via Drive API).
 *                             If unset, processing halts with a clear error.
 *   DEFAULT_CLIENT_NAME     — Fallback client_name if not parsed from subject
 *   DEFAULT_TENANT          — Fallback X-Tenant value (default: "migratehq")
 *
 * Target schema strategy:
 *   The /api/ingest endpoint requires BOTH a source file (uploaded by the sender)
 *   AND a target schema CSV (the WMS column layout to map against). The target
 *   schema does not change per-email — it is the stable WMS template for the
 *   tenant. This script fetches it from TARGET_SCHEMA_URL at runtime rather than
 *   bundling it in the script. To set this up:
 *     1. Upload the target schema CSV to Google Drive.
 *     2. Share it as "Anyone with the link can view."
 *     3. Get the direct CSV download URL and set it as TARGET_SCHEMA_URL.
 *   Alternatively, store it as a base64-encoded Script Property (TARGET_SCHEMA_B64)
 *   for environments without Drive access. See _getTargetSchemaBlob() below.
 *
 * Email subject conventions (optional metadata parsing):
 *   Include "client:<name>" and/or "tenant:<name>" anywhere in the subject.
 *   Examples:
 *     "Upload client:acme tenant:migratehq"
 *     "Weekly data client:olist"
 *   If not present, DEFAULT_CLIENT_NAME and DEFAULT_TENANT are used.
 *
 * Deployment:
 *   1. Open script.google.com, create a new project, paste this file.
 *   2. Set all Script Properties under Project Settings.
 *   3. Run setupTrigger() once manually from the editor to install the cron.
 *   4. Grant Gmail and UrlFetch permissions when prompted.
 */

// ── Configuration ─────────────────────────────────────────────────────────────

/**
 * Read all config from Script Properties so nothing sensitive is hardcoded.
 * Throws clearly if a required property is missing.
 */
function _getConfig() {
  var props = PropertiesService.getScriptProperties();
  var base  = props.getProperty('MIGRATEHQ_API_BASE_URL') || 'http://localhost:8000';
  var token = props.getProperty('MIGRATEHQ_API_TOKEN')    || '';
  var label = props.getProperty('GMAIL_LABEL')            || 'MigrateHQ/Ingest';
  var schemaUrl = props.getProperty('TARGET_SCHEMA_URL')  || '';
  var schemaB64 = props.getProperty('TARGET_SCHEMA_B64')  || '';
  var defaultClient = props.getProperty('DEFAULT_CLIENT_NAME') || 'unknown-client';
  var defaultTenant = props.getProperty('DEFAULT_TENANT')      || 'migratehq';

  if (!schemaUrl && !schemaB64) {
    throw new Error(
      'TARGET_SCHEMA_URL (or TARGET_SCHEMA_B64) is not set. ' +
      'Set it in Apps Script > Project Settings > Script Properties. ' +
      'It must point to the target WMS schema CSV file.'
    );
  }

  return {
    apiBase:       base.replace(/\/$/, ''),
    apiToken:      token,
    gmailLabel:    label,
    schemaUrl:     schemaUrl,
    schemaB64:     schemaB64,
    defaultClient: defaultClient,
    defaultTenant: defaultTenant,
  };
}


// ── Trigger management ────────────────────────────────────────────────────────

/**
 * Install a 5-minute time-driven trigger for checkInbox().
 * Idempotent: does nothing if the trigger already exists.
 * Run this once manually from the Apps Script editor.
 */
function setupTrigger() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'checkInbox') {
      Logger.log('[setupTrigger] Trigger already exists — skipping creation.');
      return;
    }
  }
  ScriptApp.newTrigger('checkInbox')
    .timeBased()
    .everyMinutes(5)
    .create();
  Logger.log('[setupTrigger] Created 5-minute trigger for checkInbox().');
}

/**
 * Remove all triggers for checkInbox(). Useful when re-deploying.
 */
function removeTriggers() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'checkInbox') {
      ScriptApp.deleteTrigger(triggers[i]);
      Logger.log('[removeTriggers] Deleted trigger: ' + triggers[i].getUniqueId());
    }
  }
}


// ── Main entry point ──────────────────────────────────────────────────────────

/**
 * Main function called by the time-driven trigger.
 * Queries the configured Gmail label for unread messages and processes each one.
 */
function checkInbox() {
  var cfg = _getConfig();
  var ts  = new Date().toISOString();
  Logger.log('[checkInbox] ' + ts + ' — scanning label: ' + cfg.gmailLabel);

  var threads = GmailApp.search('label:' + _labelQuery(cfg.gmailLabel) + ' is:unread');
  Logger.log('[checkInbox] Found ' + threads.length + ' unread thread(s).');

  for (var t = 0; t < threads.length; t++) {
    var messages = threads[t].getMessages();
    for (var m = 0; m < messages.length; m++) {
      var msg = messages[m];
      if (!msg.isUnread()) continue;
      _processMessage(msg, cfg);
    }
  }
}

/**
 * Convert a label name like "MigrateHQ/Ingest" into a Gmail search-safe form.
 * Nested labels use "/" in the UI but need quoting in search queries.
 */
function _labelQuery(label) {
  // Wrap in quotes to handle "/" and spaces correctly.
  return '"' + label + '"';
}


// ── Per-message processing ────────────────────────────────────────────────────

/**
 * Process a single Gmail message: extract CSV attachment, post to API.
 * Marks as read on success or on unrecoverable user error (400).
 * Leaves unread on transient API error (5xx) so the next cycle retries.
 *
 * Error policy:
 *   - No CSV attachment     → log warning, mark read (nothing to do)
 *   - API 400               → log error, mark read (user error, don't retry)
 *   - API 422               → log error, mark read (validation/pipeline error)
 *   - API 413               → log error, mark read (file too large, user error)
 *   - API 5xx               → log warning, leave unread (retry next cycle)
 *   - Network/fetch error   → log warning, leave unread (retry next cycle)
 *   - Success (200/201)     → log info with run_id, mark read
 */
function _processMessage(msg, cfg) {
  var sender  = msg.getFrom();
  var subject = msg.getSubject();
  var msgId   = msg.getId();
  var prefix  = '[processMessage id=' + msgId + ']';

  Logger.log(prefix + ' From: ' + sender + ' | Subject: ' + subject);

  // Find CSV attachments
  var attachments = msg.getAttachments();
  var csvBlobs = [];
  for (var i = 0; i < attachments.length; i++) {
    var att = attachments[i];
    if (att.getName().toLowerCase().endsWith('.csv')) {
      csvBlobs.push(att);
    }
  }

  if (csvBlobs.length === 0) {
    Logger.log(prefix + ' WARNING: No CSV attachments found — skipping, marking as read.');
    msg.markRead();
    return;
  }

  if (csvBlobs.length > 1) {
    Logger.log(
      prefix + ' WARNING: ' + csvBlobs.length + ' CSV attachments found. ' +
      'Using only the first: ' + csvBlobs[0].getName() +
      '. Multi-attachment emails are not fully supported.'
    );
  }

  var sourceBlob = csvBlobs[0];
  Logger.log(prefix + ' Source file: ' + sourceBlob.getName() +
    ' (' + sourceBlob.getBytes().length + ' bytes)');

  // Parse metadata from subject
  var meta      = _parseSubject(subject, cfg);
  var clientName = meta.clientName;
  var tenant    = meta.tenant;
  Logger.log(prefix + ' client_name=' + clientName + ' tenant=' + tenant);

  // Fetch target schema
  var targetBlob;
  try {
    targetBlob = _getTargetSchemaBlob(cfg);
  } catch (e) {
    Logger.log(prefix + ' ERROR: Could not load target schema: ' + e.message +
      ' — leaving unread for retry.');
    return;
  }

  // POST to /api/ingest
  var result = postToIngest(sourceBlob, targetBlob, clientName, tenant, cfg);

  if (result === null) {
    // postToIngest already logged; leave unread for retry (5xx / network error)
    return;
  }

  if (result.markReadNoRetry) {
    // 400/413/422 — user error, mark read, do not retry
    Logger.log(prefix + ' Marking as read (unrecoverable error).');
    msg.markRead();
    return;
  }

  // Success
  Logger.log(prefix + ' SUCCESS run_id=' + result.run_id +
    ' columns=' + result.total_columns +
    ' accuracy=' + result.accuracy_pct);
  msg.markRead();
}


// ── Subject parsing ───────────────────────────────────────────────────────────

/**
 * Parse "client:<name>" and "tenant:<name>" tokens from the email subject.
 * Falls back to configured defaults if tokens are absent.
 */
function _parseSubject(subject, cfg) {
  var clientMatch = subject.match(/client:([^\s]+)/i);
  var tenantMatch = subject.match(/tenant:([^\s]+)/i);
  return {
    clientName: clientMatch ? clientMatch[1] : cfg.defaultClient,
    tenant:     tenantMatch ? tenantMatch[1] : cfg.defaultTenant,
  };
}


// ── Target schema retrieval ───────────────────────────────────────────────────

/**
 * Fetch the target WMS schema blob. Priority:
 *   1. TARGET_SCHEMA_URL — fetch via UrlFetchApp (HTTPS required in production)
 *   2. TARGET_SCHEMA_B64 — decode base64-encoded CSV stored as a Script Property
 *
 * Throws if neither is configured or if the fetch fails.
 */
function _getTargetSchemaBlob(cfg) {
  if (cfg.schemaUrl) {
    var response = UrlFetchApp.fetch(cfg.schemaUrl, { muteHttpExceptions: true });
    if (response.getResponseCode() !== 200) {
      throw new Error(
        'Failed to fetch target schema from ' + cfg.schemaUrl +
        ' (HTTP ' + response.getResponseCode() + ')'
      );
    }
    return response.getBlob().setName('target_schema.csv').setContentType('text/csv');
  }

  if (cfg.schemaB64) {
    var bytes = Utilities.base64Decode(cfg.schemaB64);
    return Utilities.newBlob(bytes, 'text/csv', 'target_schema.csv');
  }

  throw new Error('No target schema configured (TARGET_SCHEMA_URL and TARGET_SCHEMA_B64 are both empty).');
}


// ── API call ──────────────────────────────────────────────────────────────────

/**
 * POST source + target files to /api/ingest as multipart/form-data.
 *
 * Returns:
 *   - On success: the parsed JSON response body (includes run_id, total_columns, etc.)
 *   - On 4xx user error: { markReadNoRetry: true } to signal "mark read, don't retry"
 *   - On 5xx / network error: null to signal "leave unread, retry next cycle"
 *
 * @param {Blob}   sourceBlob  - Gmail attachment blob (source CSV)
 * @param {Blob}   targetBlob  - Target schema CSV blob
 * @param {string} clientName  - client_name form field value
 * @param {string} tenant      - X-Tenant header value
 * @param {object} cfg         - Config object from _getConfig()
 */
function postToIngest(sourceBlob, targetBlob, clientName, tenant, cfg) {
  var url = cfg.apiBase + '/api/ingest';

  var headers = {
    'X-Tenant': tenant,
  };
  if (cfg.apiToken) {
    headers['Authorization'] = 'Bearer ' + cfg.apiToken;
  }

  var payload = {
    'source_file':  sourceBlob,
    'target_file':  targetBlob,
    'client_name':  clientName,
  };

  var options = {
    method:             'post',
    headers:            headers,
    payload:            payload,      // UrlFetchApp handles multipart encoding
    muteHttpExceptions: true,
  };

  var response;
  try {
    response = UrlFetchApp.fetch(url, options);
  } catch (e) {
    Logger.log('[postToIngest] Network error: ' + e.message + ' — will retry next cycle.');
    return null;
  }

  var code = response.getResponseCode();
  var body = response.getContentText();

  Logger.log('[postToIngest] HTTP ' + code + ' | body: ' + body.substring(0, 500));

  if (code >= 200 && code < 300) {
    try {
      return JSON.parse(body);
    } catch (e) {
      Logger.log('[postToIngest] WARNING: Could not parse success response as JSON: ' + body);
      return { run_id: 'unknown', total_columns: null, accuracy_pct: null };
    }
  }

  if (code === 400 || code === 413 || code === 422) {
    // User/validation error — log and signal "mark read, don't retry"
    Logger.log('[postToIngest] ERROR ' + code + ' (user error): ' + body);
    return { markReadNoRetry: true };
  }

  if (code === 404) {
    // Unknown tenant — also a config/user error, don't retry
    Logger.log('[postToIngest] ERROR 404 (unknown tenant or endpoint not found): ' + body);
    return { markReadNoRetry: true };
  }

  // 5xx or anything unexpected — leave unread, retry next cycle
  Logger.log('[postToIngest] WARNING HTTP ' + code + ' (server error) — leaving unread for retry: ' + body);
  return null;
}


// ── Manual test helpers ───────────────────────────────────────────────────────

/**
 * Manually test the API connection without processing any email.
 * Run from the Apps Script editor to verify config + connectivity.
 */
function testApiConnection() {
  var cfg = _getConfig();
  var url = cfg.apiBase + '/health';
  Logger.log('[testApiConnection] GET ' + url);
  try {
    var response = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
    Logger.log('[testApiConnection] HTTP ' + response.getResponseCode() +
      ' | ' + response.getContentText());
  } catch (e) {
    Logger.log('[testApiConnection] FAILED: ' + e.message);
  }
}

/**
 * List the first N unread messages in the configured label without processing them.
 * Useful for verifying the Gmail label name and trigger scope.
 */
function previewInbox() {
  var cfg     = _getConfig();
  var threads = GmailApp.search('label:' + _labelQuery(cfg.gmailLabel) + ' is:unread');
  Logger.log('[previewInbox] ' + threads.length + ' unread thread(s) in "' + cfg.gmailLabel + '"');
  var limit = Math.min(threads.length, 5);
  for (var i = 0; i < limit; i++) {
    var msg = threads[i].getMessages()[0];
    Logger.log('  [' + i + '] From: ' + msg.getFrom() +
      ' | Subject: ' + msg.getSubject() +
      ' | Attachments: ' + msg.getAttachments().length);
  }
}
