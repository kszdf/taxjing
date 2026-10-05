var store = require('../../utils/store.js');
var request = require('../../utils/request.js');

Page({
  data: {
    openid: '',
    balance: 0,
    inviteCode: '',
    level: 1
  },
  onShow: function () {
    var openid = store.getOpenid();
    this.setData({ openid: openid });
    if (openid) {
      var that = this;
      request.request('/api/state?openid=' + openid, 'GET')
        .then(function (res) {
          that.setData({
            balance: res.balance,
            inviteCode: res.invite_code,
            level: res.level
          });
        })
        .catch(function () {});
    }
  },
  onGoPoints: function () {
    wx.navigateTo({ url: '/pages/points/points' });
  },
  onGoKefu: function () {
    wx.navigateTo({ url: '/pages/kefu/kefu' });
  },
  onAbout: function () {
    wx.showModal({
      title: '关于慧根堂·数字财税助手',
      content: '本小程序由慧根堂提供财税 AI 服务，基于金税四期与以数治税背景，提供智能问答、政策检索与人工咨询。',
      showCancel: false,
      confirmText: '知道了'
    });
  },
  onCopyOpenid: function () {
    var id = this.data.openid;
    if (!id) {
      return;
    }
    wx.setClipboardData({
      data: id,
      success: function () {
        wx.showToast({ title: 'openid已复制', icon: 'none' });
      }
    });
  }
});
