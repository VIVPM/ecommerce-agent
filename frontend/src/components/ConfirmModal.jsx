import React, { useEffect, useRef } from 'react';

/**
 * One confirmation dialog for every destructive or irreversible action —
 * deleting a chat, placing an order, cancelling one.
 *
 * Replaces window.confirm, which was used for chat deletion. Beyond looking
 * like a different application, the native dialog blocks the whole JS thread
 * (so a streaming answer stalls behind it), cannot say which button is the
 * dangerous one, and is styled by the browser rather than the product.
 *
 * Render it unconditionally and drive it with `open`; it returns null when
 * closed, so callers do not need their own guard.
 */
const ConfirmModal = ({
  open,
  title,
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  danger = false,
  busy = false,
  onConfirm,
  onCancel,
}) => {
  const confirmRef = useRef(null);

  // Escape closes, and focus lands on the confirm button so the dialog is
  // usable without a mouse. The listener is on document because the overlay is
  // not focused until something inside it is.
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => {
      if (e.key === 'Escape' && !busy) onCancel?.();
    };
    document.addEventListener('keydown', onKey);
    confirmRef.current?.focus();
    return () => document.removeEventListener('keydown', onKey);
  }, [open, busy, onCancel]);

  if (!open) return null;

  return (
    <div
      className="modal-overlay"
      // Clicking the backdrop cancels, but only the backdrop itself — without
      // the target check, a click that starts inside the dialog and drifts out
      // would dismiss it.
      onClick={(e) => { if (e.target === e.currentTarget && !busy) onCancel?.(); }}
    >
      <div className="modal-card" role="alertdialog" aria-modal="true" aria-label={title}>
        <h3 className="modal-title">{title}</h3>
        {message && <p className="modal-message">{message}</p>}
        <div className="modal-actions">
          <button type="button" className="modal-btn" onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </button>
          <button
            ref={confirmRef}
            type="button"
            className={`modal-btn modal-btn-primary${danger ? ' modal-btn-danger' : ''}`}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? 'Working…' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
};

export default ConfirmModal;
