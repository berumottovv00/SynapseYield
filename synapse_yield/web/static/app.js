/* SynapseYield 聊天前端 */

const sessionId = crypto.randomUUID();
const ws = new WebSocket(`ws://${location.host}/ws/${sessionId}`);

const $messages = document.getElementById('messages');
const $input    = document.getElementById('userInput');

// ── WebSocket 事件 ────────────────────────────────────────────────────────────

ws.onopen  = () => console.log('WS connected:', sessionId);
ws.onerror = () => appendBubble('连接出错，请刷新页面。', 'error');
ws.onclose = () => appendBubble('连接已断开。', 'error');

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  switch (msg.type) {
    case 'thinking':     appendBubble(msg.text, 'thinking'); break;
    case 'summary':      appendBubble('📊 ' + msg.text, 'system'); break;
    case 'message':      appendBubble(msg.text, 'system'); break;
    case 'error':        appendBubble('⚠️ ' + msg.text, 'error'); break;
    case 'executing':    appendBubble('⏳ ' + msg.text, 'thinking'); break;
    case 'done':         appendBubble('✅ ' + msg.text, 'success'); break;
    case 'picks':        renderPicks(msg); break;
    case 'order_result': renderOrderResult(msg.data); break;
  }
};

// ── 发送用户输入 ───────────────────────────────────────────────────────────────
// 前端不再区分「选股」/「refine」/「execute」，统一发 type:"message"
// 由 Harness Agent 根据报告状态和文本内容判断意图

function sendText() {
  const text = $input.value.trim();
  if (!text) return;
  appendBubble(text, 'user');
  $input.value = '';
  autoResize();

  ws.send(JSON.stringify({
    type:        'message',
    text,
    account_id:  document.getElementById('accountId').value.trim(),
    reviewer:    document.getElementById('reviewer').value.trim(),
    report_path: document.getElementById('reportPath').value.trim(),
    n:           parseInt(document.getElementById('topN').value, 10),
    symbols:     parsedSymbols(),
  }));
}

function parsedSymbols() {
  const raw = document.getElementById('symbols').value.trim();
  return raw ? raw.split(/\s+/) : null;
}

// ── 渲染选股卡片 ───────────────────────────────────────────────────────────────

function renderPicks({ picks, account_id, reviewer }) {
  // refine 时替换旧卡片
  const old = $messages.querySelector('.picks-container');
  if (old) old.remove();

  const container = document.createElement('div');
  container.className = 'picks-container';

  const header = document.createElement('div');
  header.className = 'picks-header';
  header.textContent = `共 ${picks.length} 支候选，点击卡片选中，确认后提交下单`;
  container.appendChild(header);

  const selectedSet = new Set();

  picks.forEach((pick, idx) => {
    container.appendChild(buildPickCard(pick, idx, selectedSet));
  });

  // 提交栏
  const submitBar   = document.createElement('div');
  submitBar.className = 'submit-bar';

  const countLabel  = document.createElement('span');
  countLabel.className = 'selected-count';
  countLabel.textContent = '已选 0 支';

  const submitBtn   = document.createElement('button');
  submitBtn.className = 'btn-submit';
  submitBtn.textContent = '提交选中订单';
  submitBtn.disabled = true;

  submitBtn.onclick = () => {
    const cards = [...container.querySelectorAll('.pick-card')];
    const confirmed = cards
      .filter((_, i) => selectedSet.has(i))
      .map(c => c._getEditedPick());
    if (!confirmed.length) return;

    appendBubble(
      `已确认 ${confirmed.length} 支：${confirmed.map(p => p.symbol).join('、')}`,
      'user',
    );

    // 审批消息由 Harness Agent 的 wait_approval 节点接收并 resume
    ws.send(JSON.stringify({ type: 'approve', picks: confirmed }));

    container.querySelectorAll('.pick-card').forEach(c => {
      c.style.pointerEvents = 'none';
      c.style.opacity = '0.6';
    });
    submitBtn.disabled = true;
  };

  submitBar.appendChild(countLabel);
  submitBar.appendChild(submitBtn);
  container.appendChild(submitBar);

  container._refresh = () => {
    const n = selectedSet.size;
    countLabel.textContent = `已选 ${n} 支`;
    submitBtn.disabled = n === 0;
  };

  $messages.appendChild(container);
  scrollBottom();
}

function buildPickCard(pick, idx, selectedSet) {
  const card = document.createElement('div');
  card.className = 'pick-card';

  const pct = Math.round(parseFloat(pick.confidence || 0) * 100);

  card.innerHTML = `
    <div class="card-top">
      <span class="symbol-badge">${pick.symbol}</span>
      <span class="action-tag ${pick.action}">${pick.action === 'BUY' ? '买入' : '卖出'}</span>
      <div class="confidence-bar-wrap">
        <div class="confidence-label">置信度 ${pct}%</div>
        <div class="confidence-bar">
          <div class="confidence-fill" style="width:${pct}%"></div>
        </div>
      </div>
    </div>

    <div class="pick-grid">
      <div class="pick-field">
        <label class="pf-label">数量（股）</label>
        <input class="pf-input" data-field="quantity" type="number" min="1" step="1"
               value="${pick.quantity ?? ''}" placeholder="股数">
      </div>
      <div class="pick-field">
        <label class="pf-label">限价</label>
        <input class="pf-input" data-field="suggested_limit_price" type="number" min="0" step="0.01"
               value="${pick.suggested_limit_price ?? ''}" placeholder="市价">
      </div>
      <div class="pick-field">
        <label class="pf-label">止盈价</label>
        <input class="pf-input tp" data-field="take_profit_price" type="number" min="0" step="0.01"
               value="${pick.take_profit_price ?? ''}" placeholder="可选">
      </div>
      <div class="pick-field">
        <label class="pf-label">止损价</label>
        <input class="pf-input sl" data-field="stop_loss_price" type="number" min="0" step="0.01"
               value="${pick.stop_loss_price ?? ''}" placeholder="可选">
      </div>
    </div>

    <div class="pick-rationale">${pick.rationale}</div>

    <div class="tags-row">
      ${(pick.key_catalysts || []).map(c => `<span class="tag">${c}</span>`).join('')}
      ${(pick.risk_notes   || []).map(r => `<span class="tag risk">⚠ ${r}</span>`).join('')}
    </div>

    <div class="pick-actions">
      <button class="btn btn-skip">跳过</button>
      <button class="btn btn-confirm">确认下单</button>
    </div>
  `;

  // 读取用户编辑后的值，合并回原始 pick 对象
  card._getEditedPick = () => {
    const edited = { ...pick };
    card.querySelectorAll('.pf-input').forEach(input => {
      const field = input.dataset.field;
      const val = input.value.trim();
      if (val === '') {
        edited[field] = null;
      } else if (field === 'quantity') {
        edited[field] = parseInt(val, 10);
      } else {
        edited[field] = parseFloat(val);
      }
    });
    return edited;
  };

  card.addEventListener('click', (e) => {
    if (e.target.classList.contains('btn-skip') ||
        e.target.classList.contains('btn-confirm') ||
        e.target.tagName === 'INPUT') return;
    toggleCard(card, idx, selectedSet);
  });
  card.querySelector('.btn-confirm').addEventListener('click', () =>
    toggleCard(card, idx, selectedSet));
  card.querySelector('.btn-skip').addEventListener('click', () => {
    selectedSet.delete(idx);
    card.classList.remove('selected');
    card.parentElement._refresh?.();
  });

  return card;
}

function toggleCard(card, idx, selectedSet) {
  if (card.classList.contains('selected')) {
    card.classList.remove('selected');
    selectedSet.delete(idx);
  } else {
    card.classList.add('selected');
    selectedSet.add(idx);
  }
  card.parentElement._refresh?.();
}

// ── 渲染下单结果 ───────────────────────────────────────────────────────────────

function renderOrderResult(data) {
  const card = document.createElement('div');
  card.className = 'result-card';

  const proposal = data.proposal || {};
  const symbol   = proposal.symbol || '—';
  const ok = data.order_status && !['RISK_REJECTED', 'FAILED'].includes(data.order_status);

  card.innerHTML = `
    <div class="rc-title">
      ${symbol}
      <span class="rc-status ${ok ? 'ok' : 'fail'}">${ok ? '下单成功' : '下单失败'}</span>
    </div>
    <div class="result-row"><span>方向</span><span>${proposal.action ?? '—'}</span></div>
    <div class="result-row"><span>数量</span><span>${proposal.quantity ?? '—'} 股</span></div>
    <div class="result-row"><span>限价</span><span>${proposal.limit_price ?? '市价'}</span></div>
    <div class="result-row"><span>订单状态</span><span>${data.order_status ?? '—'}</span></div>
    <div class="result-row"><span>Broker 订单号</span><span>${data.broker_order_id ?? '—'}</span></div>
    <div class="result-row"><span>说明</span><span>${data.message ?? ''}</span></div>
    ${data.take_profit_order_id || data.stop_loss_order_id ? `
    <div class="bracket-row">
      ${data.take_profit_order_id ? `<div class="br-item"><span class="br-label tp">止盈</span>${data.take_profit_broker_order_id ?? data.take_profit_order_id}</div>` : ''}
      ${data.stop_loss_order_id   ? `<div class="br-item"><span class="br-label sl">止损</span>${data.stop_loss_broker_order_id   ?? data.stop_loss_order_id}</div>`   : ''}
    </div>` : ''}
  `;

  $messages.appendChild(card);
  scrollBottom();
}

// ── 工具函数 ──────────────────────────────────────────────────────────────────

function appendBubble(html, cls) {
  const div = document.createElement('div');
  div.className = `bubble ${cls}`;
  div.innerHTML = html;
  $messages.appendChild(div);
  scrollBottom();
}

function scrollBottom() {
  $messages.scrollTop = $messages.scrollHeight;
}

function autoResize() {
  $input.style.height = 'auto';
  $input.style.height = $input.scrollHeight + 'px';
}

$input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendText();
  }
});
$input.addEventListener('input', autoResize);
