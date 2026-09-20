/* 海龟汤管理页。
 *
 * 页面跑在 AstrBot 面板的 iframe 里，自身没有登录态，所以不能直接 fetch 后端，
 * 要通过 postMessage 让父窗口代发请求。桥只提供 api:get / api:post。
 *
 * 发出去的每条消息都必须带 kind: 'request'，面板的 handleWindowMessage 只认
 * 'ready' 和 'request' 两种，漏掉它消息会被静默丢弃，然后这里等到超时。
 *
 * 汤底永远单独请求、且需要点一下才显示 —— 管理员翻题库时不该被剧透。
 */
(() => {
  'use strict';

  const CHANNEL = 'astrbot-plugin-page';
  const DEFAULT_TIMEOUT = 120000;
  const pending = new Map();
  let seq = 0;

  window.addEventListener('message', (event) => {
    const msg = event.data;
    if (!msg || msg.channel !== CHANNEL) return;

    if (msg.kind === 'context') {
      applyTheme(msg.context?.isDark ? 'dark' : 'light');
      return;
    }
    if (msg.kind !== 'response') return;

    const slot = pending.get(msg.requestId);
    if (!slot) return;
    clearTimeout(slot.timer);
    pending.delete(msg.requestId);
    msg.ok ? slot.resolve(msg.data) : slot.reject(new Error(msg.error || '请求失败'));
  });

  function bridge(action, payload, timeout = DEFAULT_TIMEOUT) {
    return new Promise((resolve, reject) => {
      const requestId = `soupai-${Date.now()}-${++seq}`;
      const timer = setTimeout(() => {
        if (pending.has(requestId)) {
          pending.delete(requestId);
          reject(new Error('请求超时'));
        }
      }, timeout);
      pending.set(requestId, { resolve, reject, timer });
      parent.postMessage(
        { channel: CHANNEL, kind: 'request', requestId, action, ...payload },
        '*',
      );
    });
  }

  const api = {
    get: (endpoint, params, timeout) =>
      bridge('api:get', { endpoint, params: params || {} }, timeout),
    post: (endpoint, body, timeout) =>
      bridge('api:post', { endpoint, body: body || {} }, timeout),
  };

  // ─────────────────────────────────────────── 主题
  //
  // 面板用两条路告诉我们它是深是浅：iframe 地址上的 ?theme=，以及 context
  // 消息里的 isDark（切换主题时会再发一次）。都拿不到才退回系统偏好。

  const systemDark = window.matchMedia('(prefers-color-scheme: dark)');
  let themePinned = false;

  function applyTheme(theme) {
    themePinned = true;
    document.documentElement.dataset.theme = theme;
  }

  function initTheme() {
    const fromUrl = new URLSearchParams(location.search).get('theme');
    if (fromUrl === 'dark' || fromUrl === 'light') {
      applyTheme(fromUrl);
    } else {
      document.documentElement.dataset.theme = systemDark.matches ? 'dark' : 'light';
    }
    // 面板没发话时才跟着系统走，发过就以面板为准
    systemDark.addEventListener('change', (e) => {
      if (!themePinned) document.documentElement.dataset.theme = e.matches ? 'dark' : 'light';
    });
    // 面板在 iframe onload 时发一次 context，但那时这段可能还没跑完；
    // 主动报 ready 让它补发，locale 变化时也靠这条链路
    parent.postMessage({ channel: CHANNEL, kind: 'ready' }, '*');
  }

  // ─────────────────────────────────────────── 状态

  const state = {
    tab: 'stories',
    source: 'network',
    page: 1,
    pageSize: 20,
    total: 0,
    keyword: '',
    searchAnswer: false,
    session: '',
    sources: [],
    sessions: [],
    editing: null, // null=新增
  };

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };

  let toastTimer;
  function toast(message, kind = 'info') {
    const box = $('#toast');
    box.textContent = message;
    box.className = `toast toast-${kind}`;
    box.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { box.hidden = true; }, 3200);
  }

  async function guard(promise, okMessage) {
    try {
      const data = await promise;
      if (okMessage) toast(okMessage, 'ok');
      return data;
    } catch (err) {
      toast(err.message || String(err), 'error');
      throw err;
    }
  }

  /* 页面内的确认弹窗。
   *
   * 不能用 window.confirm：面板给 iframe 的 sandbox 是
   * "allow-scripts allow-forms allow-downloads"，没有 allow-modals，
   * 浏览器会忽略原生弹窗并让 confirm() 直接返回 false —— 于是所有需要
   * 确认的操作都会被静默取消，按钮看着像点了没反应。
   */
  let confirmResolve = null;
  function confirmDialog(title, detail, { danger = true, okText = '确定' } = {}) {
    $('#confirmTitle').textContent = title;
    const detailBox = $('#confirmDetail');
    detailBox.textContent = detail || '';
    detailBox.hidden = !detail;
    const okBtn = $('#confirmOk');
    okBtn.textContent = okText;
    okBtn.className = danger ? 'btn btn-danger' : 'btn btn-primary';
    $('#confirmModal').hidden = false;
    okBtn.focus();
    return new Promise((resolve) => { confirmResolve = resolve; });
  }

  function closeConfirm(answer) {
    $('#confirmModal').hidden = true;
    if (confirmResolve) {
      confirmResolve(answer);
      confirmResolve = null;
    }
  }

  /* 列表类面板的加载态。切换 tab、翻页时先垫一屏骨架，
   * 否则慢请求期间旧内容一直挂着，看不出在加载。 */
  function showLoading(selector, rows = 3) {
    const box = $(selector);
    box.innerHTML = '';
    for (let i = 0; i < rows; i++) box.appendChild(el('div', 'skeleton'));
  }

  // ─────────────────────────────────────────── 题库

  async function loadOverview() {
    const data = await api.get('overview', state.session ? { session: state.session } : {});
    state.sources = data.sources || [];
    state.sessions = data.sessions || [];
    const snapshot = JSON.stringify(data);
    if ($('#statRow').dataset.snapshot !== snapshot) {
      renderStats(data);
      renderSourceSeg();
      renderSessionFilter();
      $('#statRow').dataset.snapshot = snapshot;
    }
    return data;
  }

  function renderStats(data) {
    const row = $('#statRow');
    row.innerHTML = '';
    state.sources.forEach((s) => {
      const card = el('div', 'stat');
      card.appendChild(el('div', 'stat-label', s.label + (s.readonly ? ' · 只读' : '')));
      card.appendChild(el('div', 'stat-value', String(s.total)));
      const sub = state.session
        ? `本会话已出 ${s.used}`
        : `所有会话共出过 ${s.used_all_sessions}`;
      card.appendChild(el('div', 'stat-sub', s.hidden ? `${sub} · 屏蔽 ${s.hidden}` : sub));
      row.appendChild(card);
    });
    const card = el('div', 'stat');
    card.appendChild(el('div', 'stat-label', '进行中对局'));
    card.appendChild(el('div', 'stat-value', String(data.active_games || 0)));
    card.appendChild(el('div', 'stat-sub', `判定引擎 ${data.judge_engine || 'llm'}`));
    row.appendChild(card);
  }

  function renderSourceSeg() {
    const seg = $('#sourceSeg');
    seg.innerHTML = '';
    state.sources.forEach((s) => {
      const btn = el('button', 'seg-item' + (s.source === state.source ? ' is-active' : ''), s.label);
      btn.onclick = () => {
        state.source = s.source;
        state.page = 1;
        renderSourceSeg();
        refreshCurrentTab();
      };
      seg.appendChild(btn);
    });
  }

  function renderSessionFilter() {
    const sel = $('#sessionFilter');
    const prev = state.session;
    sel.innerHTML = '';
    sel.appendChild(new Option('全部会话', ''));
    state.sessions.forEach((s) => {
      sel.appendChild(new Option(`${s.label}（${s.used}）`, s.session));
    });
    sel.value = prev;
  }

  function currentSource() {
    return state.sources.find((s) => s.source === state.source) || {};
  }

  async function loadStories() {
    const selection = JSON.stringify([state.source, state.page, state.keyword, state.searchAnswer, state.session]);
    const params = {
      source: state.source,
      page: state.page,
      page_size: state.pageSize,
    };
    if (state.keyword) params.q = state.keyword;
    if (state.searchAnswer) params.search_answer = '1';
    if (state.session) params.session = state.session;

    if (!$('#list').children.length) showLoading('#list', 4);
    const data = await api.get('stories', params);
    if (selection !== JSON.stringify([state.source, state.page, state.keyword, state.searchAnswer, state.session])) return;
    const snapshot = JSON.stringify([selection, data]);
    if ($('#list').dataset.snapshot === snapshot) return;
    state.total = data.total;
    renderList(data);
    $('#list').dataset.snapshot = snapshot;
    $('#list').dataset.selection = selection;
  }

  function renderList(data) {
    const list = $('#list');
    list.innerHTML = '';
    if (!data.items.length) {
      list.appendChild(el('div', 'empty', state.keyword ? '没有匹配的题目' : '这个题库还是空的'));
    }
    data.items.forEach((item) => list.appendChild(renderStory(item, data.readonly)));

    const pages = Math.max(Math.ceil(data.total / data.page_size), 1);
    $('#pageInfo').textContent = `第 ${data.page} / ${pages} 页 · 共 ${data.total} 题`;
    $('#prev').disabled = data.page <= 1;
    $('#next').disabled = data.page >= pages;
  }

  function renderStory(item, readonly) {
    const card = el('div', 'card' + (item.hidden ? ' is-hidden' : ''));

    const head = el('div', 'card-head');
    const tags = el('div', 'tags');
    if (item.used) tags.appendChild(el('span', 'tag tag-used', state.session ? '本会话已出' : '出过'));
    if (item.hidden) tags.appendChild(el('span', 'tag tag-muted', '已屏蔽'));
    head.appendChild(tags);
    head.appendChild(el('span', 'card-id', `#${item.index}`));
    card.appendChild(head);

    card.appendChild(el('p', 'puzzle', item.puzzle));

    // 汤底默认折叠，点一次才拉取
    const answerBox = el('div', 'answer');
    const reveal = el('button', 'btn btn-ghost btn-sm', `查看汤底（${item.answer_preview_len} 字）`);
    reveal.onclick = async () => {
      reveal.disabled = true;
      reveal.textContent = '加载中…';
      try {
        const data = await api.get('story/answer', { source: state.source, id: item.id });
        answerBox.innerHTML = '';
        answerBox.appendChild(el('p', 'answer-text', data.answer));
        const hide = el('button', 'btn btn-ghost btn-sm', '收起');
        hide.onclick = () => renderAnswerCollapsed();
        answerBox.appendChild(hide);
      } catch (err) {
        toast(err.message, 'error');
        renderAnswerCollapsed();
      }
    };
    function renderAnswerCollapsed() {
      answerBox.innerHTML = '';
      const btn = el('button', 'btn btn-ghost btn-sm', `查看汤底（${item.answer_preview_len} 字）`);
      btn.onclick = reveal.onclick;
      answerBox.appendChild(btn);
    }
    answerBox.appendChild(reveal);
    card.appendChild(answerBox);

    const actions = el('div', 'card-actions');
    if (!readonly) {
      const edit = el('button', 'btn btn-sm', '编辑');
      edit.onclick = () => openEditor(item);
      actions.appendChild(edit);

      const del = el('button', 'btn btn-sm btn-danger', '删除');
      del.onclick = async () => {
        const ok = await confirmDialog('删除这道题？', item.puzzle, { okText: '删除' });
        if (!ok) return;
        await guard(api.post('story/delete', { source: state.source, id: item.id }), '已删除');
        await refreshCurrentTab();
      };
      actions.appendChild(del);
    }

    const hideBtn = el('button', 'btn btn-sm', item.hidden ? '取消屏蔽' : '屏蔽');
    hideBtn.title = '屏蔽后不会再被抽到，但题目仍保留';
    hideBtn.onclick = async () => {
      await guard(
        api.post('story/hide', { source: state.source, id: item.id, hidden: !item.hidden }),
        item.hidden ? '已取消屏蔽' : '已屏蔽',
      );
      await refreshCurrentTab();
    };
    actions.appendChild(hideBtn);

    if (state.session) {
      const mark = el('button', 'btn btn-sm', item.used ? '标为未出' : '标为已出');
      mark.onclick = async () => {
        await guard(
          api.post('usage/mark', {
            source: state.source, id: item.id, session: state.session, used: !item.used,
          }),
          '已更新',
        );
        await refreshCurrentTab();
      };
      actions.appendChild(mark);
    }

    card.appendChild(actions);
    return card;
  }

  // ─────────────────────────────────────────── 会话

  async function loadSessions() {
    if (!$('#sessionList').children.length) showLoading('#sessionList', 2);
    const data = await api.get('overview', {});
    state.sessions = data.sessions || [];
    const list = $('#sessionList');
    const snapshot = JSON.stringify(state.sessions);
    if (list.dataset.snapshot === snapshot) return;
    list.dataset.snapshot = snapshot;
    list.innerHTML = '';
    if (!state.sessions.length) {
      list.appendChild(el('div', 'empty', '还没有任何会话出过题'));
      return;
    }
    state.sessions.forEach((s) => {
      const card = el('div', 'card');
      const head = el('div', 'card-head');
      head.appendChild(el('strong', null, s.label));
      if (s.playing) head.appendChild(el('span', 'tag tag-live', '进行中'));
      card.appendChild(head);
      card.appendChild(el('p', 'muted mono', s.session));
      card.appendChild(el('p', null, `已出过 ${s.used} 题`));

      const actions = el('div', 'card-actions');
      const view = el('button', 'btn btn-sm', '在题库中查看');
      view.onclick = () => {
        state.session = s.session;
        state.page = 1;
        switchTab('stories');
        renderSessionFilter();
      };
      actions.appendChild(view);

      const reset = el('button', 'btn btn-sm btn-danger', '重置该会话');
      reset.onclick = async () => {
        const ok = await confirmDialog(
          `重置「${s.label}」的出题记录？`,
          '这些题在该会话中会重新可用，其他会话不受影响。',
          { okText: '重置' },
        );
        if (!ok) return;
        await guard(api.post('usage/reset', { session: s.session }), '已重置');
        refreshCurrentTab();
      };
      actions.appendChild(reset);
      card.appendChild(actions);
      list.appendChild(card);
    });
  }

  // ─────────────────────────────────────────── 对局

  /* 每一问是谁判的。judged_by 由 judge_question 写入：Jev 直接给出判定、
   * Jev 没成改用 LLM、或者本来就配置走 LLM。旧记录没有这个字段。 */
  function judgedByTag(by) {
    if (!by || !by.engine) return null;
    if (by.engine === 'jev') {
      const c = typeof by.confidence === 'number' ? ` ${by.confidence.toFixed(2)}` : '';
      return el('span', 'tag tag-jev', `Jev${c}`);
    }
    if (by.engine === 'llm') {
      return by.fallback
        ? el('span', 'tag tag-fallback', 'LLM · Jev 回退')
        : el('span', 'tag tag-llm', 'LLM');
    }
    return el('span', 'tag tag-muted', '判定失败');
  }

  function formatPercent(value) {
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1
      ? `${(value * 100).toFixed(1)}%` : '未提供';
  }

  function judgeSummary(history) {
    const n = { jev: 0, fallback: 0, llm: 0, bad: 0 };
    history.forEach((qa) => {
      const by = qa.judged_by;
      if (!by || !by.engine) return;
      if (by.engine === 'jev') n.jev++;
      else if (by.engine === 'llm') by.fallback ? n.fallback++ : n.llm++;
      else n.bad++;
    });
    const parts = [];
    if (n.jev) parts.push(`Jev ${n.jev}`);
    if (n.fallback) parts.push(`回退 LLM ${n.fallback}`);
    if (n.llm) parts.push(`LLM ${n.llm}`);
    if (n.bad) parts.push(`判定失败 ${n.bad}`);
    return parts.join(' · ');
  }

  async function loadGames() {
    const currentList = $('#gameList');
    if (!currentList.children.length) showLoading('#gameList', 2);
    const data = await api.get('games', {});
    const snapshot = JSON.stringify(data);
    if (currentList.dataset.snapshot === snapshot) return;
    // Rebuild offscreen, keeping disclosure state and the user's current scroll.
    const scrollPosition = [window.scrollX, window.scrollY];
    const expanded = new Set([...currentList.querySelectorAll('details[open]')]
      .map((details) => details.dataset.detailKey));
    const focusedDetail = document.activeElement?.closest('details')?.dataset.detailKey;
    const list = document.createDocumentFragment();
    if (!data.games.length) {
      list.appendChild(el('div', 'empty', '当前没有进行中的对局'));
    }
    data.games.forEach((g) => {
      const card = el('div', 'card');
      const head = el('div', 'card-head');
      head.appendChild(el('strong', null, g.label));
      head.appendChild(el('span', 'tag', g.difficulty));
      card.appendChild(head);
      card.appendChild(el('p', 'puzzle', g.puzzle));

      const q = g.question_limit ? `${g.question_count}/${g.question_limit}` : `${g.question_count}（不限）`;
      const h = g.hint_limit ? `${g.hint_count}/${g.hint_limit}` : '不可用';
      const v = data.verification_limit > 0
        ? `${g.verification_attempts}/${data.verification_limit}` : `${g.verification_attempts}（不限）`;
      const summary = judgeSummary(g.qa_history || []);
      card.appendChild(el('p', 'muted',
        `提问 ${q} · 提示 ${h} · 验证 ${v}${summary ? ` · 判定 ${summary}` : ''}`));

      if (g.qa_history && g.qa_history.length) {
        const details = el('details', 'qa');
        details.dataset.detailKey = `${g.key}:${g.started_at || ''}:history`;
        details.open = expanded.has(details.dataset.detailKey);
        details.appendChild(el('summary', null, `问答记录（${g.qa_history.length}）`));
        const ol = el('ol');
        g.qa_history.forEach((qa, index) => {
          const li = el('li');
          li.appendChild(el('span', 'qa-q', qa.question));
          li.appendChild(el('span', 'qa-a', qa.answer));
          const by = judgedByTag(qa.judged_by);
          if (by) li.appendChild(by);
          const judgement = qa.judged_by || {};
          const jev = judgement.jev;
          if (jev || judgement.engine === 'jev' || judgement.fallback) {
            const metrics = el('details', 'judge-details');
            metrics.dataset.detailKey = `${g.key}:${g.started_at || ''}:judge:${index}`;
            metrics.open = expanded.has(metrics.dataset.detailKey);
            metrics.appendChild(el('summary', null, 'Jev 判定详情'));
            const info = el('div', 'judge-info');
            info.appendChild(el('p', null, `Jev 原选项：${jev?.choice || '未提供'}`));
            const confidence = jev ? jev.confidence : judgement.confidence;
            info.appendChild(el('p', null,
              `整体置信度：${formatPercent(confidence)}${!jev && confidence != null ? '（旧记录）' : ''}`));
            info.appendChild(el('p', null, `采用门槛：${formatPercent(jev?.threshold)}`));
            const reasons = {
              low_confidence: '整体置信度低于采用门槛',
              request_failed: 'Jev 请求失败',
              invalid_response: 'Jev 响应格式无效',
              unknown_choice: 'Jev 返回了候选之外的选项',
            };
            const reason = jev?.reason === null && judgement.engine === 'jev'
              ? '无（已采用 Jev 判定）' : reasons[jev?.reason] || '未提供';
            info.appendChild(el('p', null, `回退原因：${reason}`));
            metrics.appendChild(info);
            metrics.appendChild(el('p', 'judge-note muted',
              '以下为 Jev 返回的选项概率；整体置信度是独立指标，不等于最高选项概率。'));
            const probabilities = el('dl', 'judge-probabilities');
            ['是', '否', '不重要', '是也不是'].forEach((choice) => {
              const row = el('div', 'judge-probability');
              if (choice === jev?.choice) row.classList.add('is-choice');
              row.appendChild(el('dt', null, choice));
              row.appendChild(el('dd', null, formatPercent(jev?.probabilities?.[choice])));
              probabilities.appendChild(row);
            });
            metrics.appendChild(probabilities);
            if (!jev) {
              metrics.appendChild(el('p', 'judge-note muted',
                '旧记录未保存 Jev 原选项、四选项概率、采用门槛和回退原因。'));
            }
            li.appendChild(metrics);
          }
          ol.appendChild(li);
        });
        details.appendChild(ol);
        card.appendChild(details);
      }

      const actions = el('div', 'card-actions');
      const end = el('button', 'btn btn-sm btn-danger', '强制结束');
      end.onclick = async () => {
        const ok = await confirmDialog(
          `强制结束「${g.label}」的对局？`,
          '对局会被直接清掉，群里不会收到任何提示，汤底也不会公布。',
          { okText: '结束' },
        );
        if (!ok) return;
        await guard(api.post('games/end', { key: g.key }), '已结束');
        refreshCurrentTab();
      };
      actions.appendChild(end);
      card.appendChild(actions);
      list.appendChild(card);
    });
    currentList.replaceChildren(list);
    currentList.dataset.snapshot = snapshot;
    if (focusedDetail) {
      [...currentList.querySelectorAll('details')]
        .find((details) => details.dataset.detailKey === focusedDetail)
        ?.querySelector('summary')?.focus({ preventScroll: true });
    }
    window.scrollTo(...scrollPosition);
  }

  // ─────────────────────────────────────────── 弹窗

  function openEditor(item) {
    state.editing = item || null;
    $('#editorTitle').textContent = item ? '编辑题目' : '新增题目';
    $('#editPuzzle').value = item ? item.puzzle : '';
    $('#editAnswer').value = '';

    // 只读题库上的「新增」会落到自定义题库，先说清楚再让人动手写
    const note = $('#editorNote');
    const readonly = !item && currentSource().readonly;
    note.textContent = readonly
      ? `${currentSource().label}是只读的，新题会存进自定义题库。`
      : '';
    note.hidden = !readonly;

    $('#editor').hidden = false;
    $('#editPuzzle').focus();

    if (item) {
      // 编辑时需要原汤底做初值，这里是明确的编辑意图，直接拉取
      api.get('story/answer', { source: state.source, id: item.id })
        .then((data) => { $('#editAnswer').value = data.answer; })
        .catch((err) => toast(err.message, 'error'));
    }
  }

  async function saveEditor() {
    const puzzle = $('#editPuzzle').value.trim();
    const answer = $('#editAnswer').value.trim();
    if (!puzzle || !answer) return toast('汤面和汤底都不能为空', 'error');

    const target = currentSource().readonly ? 'custom' : state.source;
    if (state.editing) {
      await guard(api.post('story/update', {
        source: state.source, id: state.editing.id, puzzle, answer,
      }), '已保存');
    } else {
      await guard(api.post('story/create', { source: target, puzzle, answer }), '已新增');
      if (target !== state.source) {
        state.source = target;
        renderSourceSeg();
      }
    }
    $('#editor').hidden = true;
    await refreshCurrentTab();
  }

  async function runGenerate() {
    const count = Math.min(Math.max(parseInt($('#genCount').value, 10) || 1, 1), 5);
    const box = $('#genResult');
    const btn = $('#genRun');
    btn.disabled = true;
    btn.textContent = '生成中…';
    box.hidden = false;
    box.innerHTML = '';
    box.appendChild(el('p', 'muted', `正在生成 ${count} 道题，LLM 出题较慢，请稍候…`));
    try {
      // 一道题要等 LLM 一轮完整输出，5 道叠起来能远超默认的两分钟
      const data = await api.post('story/generate', { count }, 600000);
      box.innerHTML = '';
      (data.created || []).forEach((item) => {
        const row = el('div', 'gen-item');
        row.appendChild(el('span', 'tag tag-used', '已入库'));
        row.appendChild(el('span', null, item.puzzle));
        box.appendChild(row);
      });
      (data.failed || []).forEach((reason) => {
        const row = el('div', 'gen-item');
        row.appendChild(el('span', 'tag tag-muted', '失败'));
        row.appendChild(el('span', 'muted', reason));
        box.appendChild(row);
      });
      toast(`生成完成：成功 ${(data.created || []).length} 道`, 'ok');
      state.source = 'local';
      renderSourceSeg();
      await refreshCurrentTab();
    } catch (err) {
      box.innerHTML = '';
      box.appendChild(el('p', 'error-text', err.message));
    } finally {
      btn.disabled = false;
      btn.textContent = '开始生成';
    }
  }

  // ─────────────────────────────────────────── 设置

  const cfg = {
    schema: {},        // 后端下发的配置 schema（和面板渲染用的同一份）
    values: {},        // 当前正在编辑的值
    saved: {},         // 最近一次加载/保存成功的快照，用于「撤销修改」
    providers: [],
    hasKey: false,
    dirty: false,
    revision: 0,
  };

  // provider 下拉显示「id（model）」
  function providerLabel(p) {
    return p.model ? `${p.id}（${p.model}）` : p.id;
  }

  async function loadConfig() {
    const revision = cfg.revision;
    const data = await api.get('config', {});
    if (revision !== cfg.revision) return false;
    cfg.schema = data.schema || {};
    cfg.providers = data.providers || [];
    cfg.hasKey = !!data.has_jev_api_key;
    cfg.values = { ...data.values };
    cfg.saved = { ...data.values };
    cfg.dirty = false;
    renderConfigForm();
  }

  // 和面板 ConfigItemRenderer 的 condition 语义一致：条件里的键值全部
  // 相等才显示。目前只有 judge_engine 一个条件键
  function conditionMet(cond) {
    if (!cond) return true;
    return Object.entries(cond).every(([key, expected]) => cfg.values[key] === expected);
  }

  function setValue(key, value, rerender) {
    if (cfg.values[key] === value) return;
    cfg.values[key] = value;
    cfg.dirty = true;
    cfg.revision++;
    // judge_engine 的开关会增删 Jev 区块，必须整表重绘；普通输入不要
    // 重绘，否则打字打到一半会丢焦点
    if (rerender) renderConfigForm();
  }

  function renderConfigForm() {
    const form = $('#configForm');
    form.innerHTML = '';
    Object.entries(cfg.schema).forEach(([key, meta]) => {
      if (!conditionMet(meta.condition)) return;
      form.appendChild(renderConfigItem(key, meta));
    });
  }

  function renderConfigItem(key, meta) {
    const wrap = el('label', 'cfg-item');
    wrap.appendChild(el('span', 'cfg-desc', meta.description || key));

    let input;
    const isNumeric = meta.type === 'int' || meta.type === 'float';
    if (meta._special === 'select_provider') {
      input = el('select', 'input');
      input.appendChild(new Option(
        ['hint_llm_provider', 'verify_llm_provider'].includes(key)
          ? '（跟随判断问答 LLM）' : '（使用系统默认）',
        '',
      ));
      cfg.providers.forEach((p) => input.appendChild(new Option(providerLabel(p), p.id)));
      input.value = cfg.values[key] || '';
      input.onchange = (e) => setValue(key, e.target.value);
    } else if (meta.options) {
      input = el('select', 'input');
      const labels = meta.labels || meta.options;
      meta.options.forEach((opt, i) => input.appendChild(new Option(labels[i] || opt, opt)));
      input.value = cfg.values[key] || '';
      input.onchange = (e) => setValue(key, e.target.value, true);
    } else if (meta.type === 'bool') {
      input = el('input', 'input');
      input.type = 'checkbox';
      input.checked = !!cfg.values[key];
      // label 包着 checkbox 时点文字也会切换它，只触发一次 change，直接用
      input.onchange = (e) => setValue(key, e.target.checked);
      wrap.classList.add('cfg-bool');
    } else {
      input = el('input', 'input');
      input.type = meta.secret ? 'password' : isNumeric ? 'number' : 'text';
      if (meta.type === 'int') input.step = '1';
      if (meta.type === 'float') input.step = '0.05';
      if (key === 'jev_api_key') {
        input.placeholder = cfg.hasKey ? '已设置（留空表示不修改）' : '未设置';
        input.autocomplete = 'new-password';
      }
      input.value = cfg.values[key] ?? '';
      input.oninput = (e) => setValue(key, isNumeric
        ? (e.target.value === '' ? '' : Number(e.target.value))
        : e.target.value);
      if (isNumeric) {
        // 数字框被清空后离开输入状态就回退成上次保存的值，避免存进空值
        input.onblur = () => {
          if (input.value === '') {
            input.value = cfg.saved[key] ?? '';
            cfg.values[key] = cfg.saved[key] ?? '';
          }
        };
      }
    }
    wrap.appendChild(input);

    if (meta.hint) wrap.appendChild(el('span', 'cfg-hint', meta.hint));
    return wrap;
  }

  async function saveConfig() {
    const btn = $('#cfgSave');
    btn.disabled = true;
    btn.textContent = '保存中…';
    try {
      const payload = { ...cfg.values };
      // 空串表示「没改过」，不提交；显式清空走下面的 clear 字段
      if (payload.jev_api_key === '') delete payload.jev_api_key;
      await guard(api.post('config/save', payload), '配置已保存，立即生效');
      await loadConfig();
    } finally {
      btn.disabled = false;
      btn.textContent = '保存配置';
    }
  }

  function resetConfig() {
    cfg.values = { ...cfg.saved };
    cfg.dirty = false;
    cfg.revision++;
    renderConfigForm();
  }

  // ─────────────────────────────────────────── 交互绑定

  let refreshing = false;
  let refreshQueued = false;

  async function refreshCurrentTab({ manual = false, automatic = false } = {}) {
    if (automatic && (!$('#autoRefresh').checked || document.hidden || state.tab === 'settings'
      || pending.size || document.querySelector('.modal:not([hidden])'))) return;
    if (refreshing) {
      if (!automatic) refreshQueued = true;
      return;
    }
    if (state.tab === 'settings' && cfg.dirty && !manual) {
      $('#refreshStatus').textContent = '设置有未保存修改，已保留当前表单；设置页暂停自动刷新。';
      return;
    }

    refreshing = true;
    const tab = state.tab;
    const button = $('#refresh');
    const status = $('#refreshStatus');
    button.disabled = true;
    button.textContent = '刷新中…';
    status.classList.remove('is-error');
    try {
      if (tab === 'settings' && cfg.dirty) {
        const confirmed = await confirmDialog('重新加载设置？', '尚未保存的修改会丢失。', { okText: '重新加载' });
        if (!confirmed) {
          status.textContent = '已保留未保存的设置修改；设置页暂停自动刷新。';
          return;
        }
      }
      if (!automatic) status.textContent = '正在刷新当前标签…';
      let preservedConfig = false;
      if (tab === 'stories') {
        const selection = JSON.stringify([state.source, state.page, state.keyword, state.searchAnswer, state.session]);
        if ($('#list').dataset.selection !== selection) {
          showLoading('#list', 4);
          delete $('#list').dataset.snapshot;
        }
        await loadOverview();
        if (state.tab === tab) await loadStories();
      } else if (tab === 'sessions') await loadSessions();
      else if (tab === 'games') await loadGames();
      else if (tab === 'settings') preservedConfig = await loadConfig() === false;

      if (state.tab === tab) {
        const note = tab === 'settings' ? ' · 设置页暂停自动刷新'
          : $('#autoRefresh').checked ? ' · 每 5 秒自动刷新' : ' · 自动刷新已关闭';
        status.textContent = preservedConfig
          ? '已保留刷新期间的设置修改；设置页暂停自动刷新。'
          : `上次刷新 ${new Date().toLocaleTimeString()}${note}`;
        if (manual) toast(preservedConfig ? '已保留当前设置修改' : '当前标签已刷新', preservedConfig ? 'info' : 'ok');
      }
    } catch (err) {
      if (state.tab === tab) {
        status.textContent = `刷新失败：${err.message || String(err)}。已保留上次显示的数据。`;
        status.classList.add('is-error');
        const panel = document.querySelector(`.panel[data-panel="${tab}"]`);
        panel.querySelectorAll('.skeleton').forEach((node) => node.remove());
      }
      if (!automatic) toast(err.message || String(err), 'error');
    } finally {
      refreshing = false;
      button.disabled = false;
      button.textContent = '刷新';
      if (refreshQueued) {
        refreshQueued = false;
        void refreshCurrentTab();
      }
    }
  }

  function switchTab(name) {
    state.tab = name;
    document.querySelectorAll('.tab').forEach((t) => {
      t.classList.toggle('is-active', t.dataset.tab === name);
    });
    document.querySelectorAll('.panel').forEach((p) => {
      p.hidden = p.dataset.panel !== name;
    });
    refreshCurrentTab();
  }

  document.querySelectorAll('.tab').forEach((tab) => {
    tab.onclick = () => switchTab(tab.dataset.tab);
  });

  let searchTimer;
  $('#search').oninput = (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.keyword = e.target.value.trim();
      state.page = 1;
      refreshCurrentTab();
    }, 260);
  };
  $('#searchAnswer').onchange = (e) => {
    state.searchAnswer = e.target.checked;
    if (state.keyword) refreshCurrentTab();
  };
  $('#sessionFilter').onchange = (e) => {
    state.session = e.target.value;
    state.page = 1;
    refreshCurrentTab();
  };
  $('#prev').onclick = () => { if (state.page > 1) { state.page--; refreshCurrentTab(); } };
  $('#next').onclick = () => { state.page++; refreshCurrentTab(); };
  $('#refresh').onclick = () => refreshCurrentTab({ manual: true });
  $('#autoRefresh').onchange = () => {
    $('#refreshStatus').textContent = $('#autoRefresh').checked
      ? '自动刷新已开启（每 5 秒）；后台、弹窗和设置编辑期间暂停。'
      : '自动刷新已关闭，可手动刷新当前标签。';
    if ($('#autoRefresh').checked) refreshCurrentTab({ automatic: true });
  };
  $('#btnCreate').onclick = () => openEditor(null);
  $('#editSave').onclick = saveEditor;
  $('#btnGenerate').onclick = () => { $('#genResult').hidden = true; $('#genModal').hidden = false; };
  $('#genRun').onclick = runGenerate;
  $('#cfgSave').onclick = saveConfig;
  $('#cfgReset').onclick = resetConfig;
  $('#resetAll').onclick = async () => {
    const ok = await confirmDialog(
      '重置所有会话的出题记录？',
      '所有群和私聊的记录都会清空，三个题库的题目都会重新变成可出。',
      { okText: '全部重置' },
    );
    if (!ok) return;
    await guard(api.post('usage/reset', {}), '已全部重置');
    refreshCurrentTab();
  };
  $('#confirmOk').onclick = () => closeConfirm(true);
  $('#confirmCancel').onclick = () => closeConfirm(false);
  // 关掉确认框必须走 closeConfirm，否则等它的 Promise 永远不落地
  function dismissModal(modal) {
    if (modal.id === 'confirmModal') closeConfirm(false);
    else modal.hidden = true;
  }

  document.querySelectorAll('[data-close]').forEach((btn) => {
    btn.onclick = () => dismissModal(btn.closest('.modal'));
  });
  document.querySelectorAll('.modal').forEach((modal) => {
    modal.onclick = (e) => { if (e.target === modal) dismissModal(modal); };
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const open = [...document.querySelectorAll('.modal')].filter((m) => !m.hidden);
      // 确认框可能盖在编辑器上面，只关最上面那层
      if (open.length) dismissModal(open[open.length - 1]);
      return;
    }
    if (e.key === 'Enter') {
      if (!$('#confirmModal').hidden) { closeConfirm(true); return; }
      // 编辑器里是多行文本框，回车要留给换行，用 Ctrl/Cmd+Enter 保存
      if (!$('#editor').hidden && (e.ctrlKey || e.metaKey)) { saveEditor(); }
    }
  });

  // 启动
  initTheme();
  refreshCurrentTab();
  const refreshTimer = setInterval(() => refreshCurrentTab({ automatic: true }), 5000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshCurrentTab({ automatic: true });
  });
  window.addEventListener('pagehide', () => clearInterval(refreshTimer));
})();
