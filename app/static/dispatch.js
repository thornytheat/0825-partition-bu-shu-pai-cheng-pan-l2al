import { request } from './client.js';

const claimForm = document.querySelector('#claim-form');
const receiptForm = document.querySelector('#receipt-form');
const table = document.querySelector('#dispatch-table');
const notice = document.querySelector('#dispatch-notice');
const refreshButton = document.querySelector('#dispatch-refresh');
const sweepButton = document.querySelector('#sweep-timeouts');

function announce(text, failure = false) {
  notice.textContent = text;
  notice.dataset.failure = String(failure);
}

function formJSON(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  for (const key of ['start', 'end', 'timeout_seconds']) {
    if (data[key] !== undefined && data[key] !== '') data[key] = Number(data[key]);
    else delete data[key];
  }
  if (data.depends_on) {
    data.depends_on = String(data.depends_on).split(',').map(s => s.trim()).filter(Boolean);
  } else {
    delete data.depends_on;
  }
  if (!data.request_id) data.request_id = `web-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  return data;
}

function render(claims) {
  if (!claims.length) {
    table.innerHTML = '<caption>暂无领槽记录</caption>';
    return;
  }
  const head = ['领槽', '分区', '批次', '时隙', '状态', '回执', '幂等重试', '操作'];
  const thead = `<thead><tr>${head.map(h => `<th>${h}</th>`).join('')}</tr></thead>`;
  const rows = claims.map(c => {
    const slot = Array.isArray(c.slot) ? `[${c.slot[0]}, ${c.slot[1]})` : '—';
    const id = String(c.id);
    return `<tr>
      <td title="${id}">${id.slice(0, 10)}</td>
      <td>${c.partition ?? c.task_id ?? '—'}</td>
      <td>${c.batch ?? c.run_id ?? '—'}</td>
      <td>${slot}</td>
      <td data-state="${c.status}">${c.status}</td>
      <td>${c.receipt_outcome ?? '—'}</td>
      <td>${c.replayed ? '是' : '否'}</td>
      <td><button type="button" data-receipt="${id}">登记回执</button></td>
    </tr>`;
  }).join('');
  table.innerHTML = `${thead}<tbody>${rows}</tbody>`;
}

async function refresh() {
  try {
    const data = await request('/api/claims');
    render(data.items || []);
  } catch (error) {
    announce(error.message, true);
  }
}

claimForm?.addEventListener('submit', event => {
  event.preventDefault();
  (async () => {
    try {
      const result = await request('/api/claims', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(formJSON(claimForm)),
      });
      announce(result.replayed ? '相同请求已处理，返回原领槽（未重复占槽）' : `已领取 ${String(result.id).slice(0, 10)}`);
      receiptForm.elements.claim_id.value = result.id;
      await refresh();
    } catch (error) {
      announce(error.message, true);
      await refresh();
    }
  })();
});

receiptForm?.addEventListener('submit', event => {
  event.preventDefault();
  (async () => {
    const payload = Object.fromEntries(new FormData(receiptForm).entries());
    try {
      const result = await request('/api/receipts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      announce(`回执已登记：${result.receipt_outcome}，槽位释放=${result.slot_released ? '是' : '否'}`);
      await refresh();
    } catch (error) {
      announce(error.message, true);
    }
  })();
});

table?.addEventListener('click', event => {
  const button = event.target.closest('[data-receipt]');
  if (button) {
    receiptForm.elements.claim_id.value = button.dataset.receipt;
    receiptForm.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
});

refreshButton?.addEventListener('click', refresh);

const issuedPick = document.querySelector('#issued-pick');
async function loadIssued() {
  try {
    const data = await request('/api/issued-tasks');
    const items = data.items || [];
    issuedPick.innerHTML = '<option value="">— 手动填写 —</option>' +
      items.map((t, i) => `<option value="${i}">${t.partition} · [${t.slot[0]},${t.slot[1]}) · ${String(t.run_id).slice(0, 8)}</option>`).join('');
    issuedPick._items = items;
  } catch (error) {
    announce(error.message, true);
  }
}
issuedPick?.addEventListener('change', () => {
  const task = issuedPick._items?.[Number(issuedPick.value)];
  if (!task) return;
  claimForm.elements.run_id.value = task.run_id;
  claimForm.elements.task_id.value = task.qualified_id;
  claimForm.elements.start.value = task.start;
  claimForm.elements.end.value = task.end;
});
loadIssued();
sweepButton?.addEventListener('click', async () => {
  try {
    const changed = await request('/api/timeouts/sweep', { method: 'POST' });
    const list = changed.items || changed || [];
    announce(`超时扫描完成：${list.length} 条标记超时，执行槽均保留不释放`);
    await refresh();
  } catch (error) {
    announce(error.message, true);
  }
});

refresh();
