var store = require('../../utils/store.js');
var request = require('../../utils/request.js');

var CAPSULES = [
  { emoji: '📚', label: '查政策', q: '帮我查一下最新的政策：' },
  { emoji: '👤', label: '问客户', q: '我们客户 ' },
  { emoji: '✅', label: '派任务', q: '帮我派个任务：' },
  { emoji: '👥', label: '问同行', q: '同行有没有人遇到过 ' },
  { emoji: '🔔', label: '提醒', q: '提醒我这个月征期别忘了申报' }
];

var SUGGEST_LABEL = {
  policy_detail: '查相关政策原文',
  community: '去同行问讨论',
  teach: '教我一下（沉淀口径）',
  mark: '标记此口径',
  promote: '沉淀为全局口径',
  task: '去机构工作台派任务',
  calendar: '看征期日历',
  subscribe: '设置提醒',
  client: '去客户体检',
  reask: '换个角度问',
  open_community: '去同行问发帖'
};

Page({
  data: {
    capsules: CAPSULES,
    question: '',
    loading: false,
    msgs: [],        // [{role:'me',text} | {role:'ai',question,card,skills,action,suggestions,teachSubmitted}]
    balance: 0,
    orgName: '',
    scrollInto: ''
  },
  onShow: function () {
    this.refreshState();
  },
  refreshState: function () {
    var openid = store.getOpenid();
    if (!openid) { return; }
    var that = this;
    var info = store.getOrgInfo();
    this.setData({ orgName: info && info.name ? info.name : '' });
    request.request('/api/state?openid=' + openid, 'GET')
      .then(function (res) {
        that.setData({ balance: res.balance || 0 });
      })
      .catch(function () {});
  },
  onInput: function (e) {
    this.setData({ question: e.detail.value });
  },
  onCapsule: function (e) {
    this.setData({ question: e.currentTarget.dataset.q || '' });
  },
  onSubmit: function () {
    var q = (this.data.question || '').trim();
    if (!q) { wx.showToast({ title: '请输入问题', icon: 'none' }); return; }
    if (this.data.loading) { return; }
    var openid = store.getOpenid();
    if (!openid) { wx.showToast({ title: '系统初始化中，请稍后再试', icon: 'none' }); return; }

    var me = { role: 'me', text: q };
    this.setData({ msgs: this.data.msgs.concat([me]), question: '', loading: true });
    this.scrollBottom();

    var that = this;
    var orgToken = store.getOrgToken();
    var doCall = orgToken
      ? request.converse('/api/converse', 'POST', { openid: openid, question: q }, orgToken)
      : request.converse('/api/converse', 'POST', { openid: openid, question: q });

    doCall.then(function (res) {
      var ai = {
        role: 'ai',
        question: q,
        card: res.card || { type: 'qa', text: res.answer || '', refs: res.policy_refs || [], locked: !!res.locked, learnable: !!res.learnable },
        skills: res.skills_called || [],
        action: res.action || null,
        suggestions: res.suggestions || [],
        teachSubmitted: false
      };
      that.setData({ msgs: that.data.msgs.concat([ai]), loading: false, balance: (res.balance != null ? res.balance : that.data.balance) });
      that.scrollBottom();
    }).catch(function () {
      that.setData({ loading: false });
    });
  },
  onTeach: function (e) {
    var idx = e.currentTarget.dataset.idx;
    var m = this.data.msgs[idx];
    if (!m || !m.card) { return; }
    if (m.teachSubmitted) { wx.showToast({ title: '已提交，待审定', icon: 'none' }); return; }
    var that = this;
    var openid = store.getOpenid();
    request.request('/api/canonical/submit', 'POST', {
      openid: openid,
      question: m.question,
      answer: m.card.text,
      policy_refs: m.card.refs || []
    }).then(function (res) {
      if (res.ok) {
        var key = 'msgs[' + idx + '].teachSubmitted';
        that.setData({ [key]: true });
        wx.showToast({ title: '已提交，待审定后锁定', icon: 'success' });
      }
    }).catch(function () {});
  },
  onAction: function (e) {
    var type = e.currentTarget.dataset.type;
    if (type === 'open_community') {
      wx.navigateTo({ url: '/pages/community/list' });
    } else if (type === 'open_calendar') {
      wx.navigateTo({ url: '/pages/calendar/calendar' });
    } else if (type === 'open_org_task' || type === 'open_org_client') {
      wx.navigateTo({ url: '/pages/org/org' });
    } else if (type === 'open_policy_detail') {
      wx.navigateTo({ url: '/pages/policy/policy' });
    }
  },
  onSuggestion: function (e) {
    var intent = e.currentTarget.dataset.intent;
    if (intent === 'teach') {
      var idx = e.currentTarget.dataset.idx;
      this.onTeach({ currentTarget: { dataset: { idx: idx } } });
      return;
    }
    if (intent === 'community') {
      wx.navigateTo({ url: '/pages/community/list' });
    } else if (intent === 'calendar' || intent === 'subscribe') {
      wx.navigateTo({ url: '/pages/calendar/calendar' });
    } else if (intent === 'task' || intent === 'client') {
      wx.navigateTo({ url: '/pages/org/org' });
    } else if (intent === 'policy_detail') {
      wx.navigateTo({ url: '/pages/policy/policy' });
    } else if (intent === 'reask' || intent === 'mark' || intent === 'promote') {
      wx.showToast({ title: '可在回答下「教我一下」沉淀口径', icon: 'none' });
    }
  },
  onClear: function () {
    this.setData({ msgs: [], scrollInto: '' });
  },
  scrollBottom: function () {
    var that = this;
    setTimeout(function () {
      that.setData({ scrollInto: 'm' + that.data.msgs.length });
    }, 80);
  }
});
