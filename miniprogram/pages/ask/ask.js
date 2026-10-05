var store = require('../../utils/store.js');
var request = require('../../utils/request.js');

Page({
  data: {
    question: '',
    loading: false,
    msgs: [],        // [{role:'me'|'ai', text, refs:[], mode, cost}]
    balance: 0,
    scrollInto: ''
  },
  onLoad: function () {
    this.refreshBalance();
  },
  onShow: function () {
    this.refreshBalance();
  },
  refreshBalance: function () {
    var openid = store.getOpenid();
    if (!openid) return;
    var that = this;
    request.request('/api/state?openid=' + openid, 'GET').then(function (res) {
      that.setData({ balance: res.balance || 0 });
    }).catch(function () {});
  },
  onInput: function (e) {
    this.setData({ question: e.detail.value });
  },
  onQuick: function (e) {
    this.setData({ question: e.currentTarget.dataset.q || '' });
    this.onSubmit();
  },
  onSubmit: function () {
    var q = (this.data.question || '').trim();
    if (!q) { wx.showToast({ title: '请输入问题', icon: 'none' }); return; }
    if (this.data.loading) return;
    var openid = store.getOpenid();
    if (!openid) { wx.showToast({ title: '系统初始化中，请稍后再试', icon: 'none' }); return; }

    var msgs = this.data.msgs.concat([{ role: 'me', text: q }]);
    this.setData({ msgs: msgs, question: '', loading: true });
    this.scrollBottom();

    var that = this;
    request.request('/api/ask', 'POST', { openid: openid, question: q, type: 'ai_deep' })
      .then(function (res) {
        var m = that.data.msgs.concat([{
          role: 'ai',
          text: res.answer || '',
          refs: res.policy_refs || [],
          mode: res.mode || '',
          cost: res.cost || 0
        }]);
        that.setData({ msgs: m, loading: false, balance: res.balance || 0 });
        that.scrollBottom();
      })
      .catch(function () {
        that.setData({ loading: false });
      });
  },
  onClear: function () {
    this.setData({ msgs: [], scrollInto: '' });
  },
  scrollBottom: function () {
    var that = this;
    // 延迟一帧，等列表渲染完再滚到底
    setTimeout(function () {
      that.setData({ scrollInto: 'm' + that.data.msgs.length });
    }, 80);
  }
});
