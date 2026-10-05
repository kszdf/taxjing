var store = require('../../utils/store.js');
var request = require('../../utils/request.js');

Page({
  data: {
    question: '',
    loading: false,
    tickets: [],
    loadingTickets: true
  },
  onShow: function () {
    this.loadTickets();
  },
  onInput: function (e) {
    this.setData({ question: e.detail.value });
  },
  onSubmit: function () {
    var q = (this.data.question || '').trim();
    if (!q) {
      wx.showToast({ title: '请输入您的问题', icon: 'none' });
      return;
    }
    var openid = store.getOpenid();
    if (!openid) {
      wx.showToast({ title: '系统初始化中，请稍后再试', icon: 'none' });
      return;
    }
    var that = this;
    this.setData({ loading: true });
    request.request('/api/human/submit', 'POST', {
      openid: openid,
      question: q
    }).then(function (res) {
      that.setData({ loading: false, question: '' });
      wx.showToast({ title: '已提交，消耗' + (res.cost || 30) + '积分', icon: 'none' });
      that.loadTickets();
    }).catch(function () {
      that.setData({ loading: false });
    });
  },
  loadTickets: function () {
    var openid = store.getOpenid();
    if (!openid) {
      this.setData({ loadingTickets: false });
      return;
    }
    var that = this;
    this.setData({ loadingTickets: true });
    request.request('/api/human/my?openid=' + openid, 'GET')
      .then(function (res) {
        that.setData({ tickets: res.tickets || [], loadingTickets: false });
      })
      .catch(function () {
        that.setData({ loadingTickets: false });
      });
  }
});
