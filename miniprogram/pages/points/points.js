var store = require('../../utils/store.js');
var request = require('../../utils/request.js');

Page({
  data: {
    balance: 0,
    level: 1,
    inviteCount: 0,
    inviteCode: '',
    todayEarn: 0,
    totalEarn: 0
  },
  onShow: function () {
    this.refresh();
  },
  refresh: function () {
    var openid = store.getOpenid();
    if (!openid) {
      return;
    }
    var that = this;
    request.request('/api/state?openid=' + openid, 'GET')
      .then(function (res) {
        that.setData({
          balance: res.balance,
          level: res.level,
          inviteCount: res.invite_count,
          inviteCode: res.invite_code,
          todayEarn: res.today_earn,
          totalEarn: res.total_earn
        });
      })
      .catch(function () {});
  },
  onGetPoints: function () {
    wx.navigateTo({ url: '/pages/kefu/kefu' });
  },
  onCopyInvite: function () {
    var code = this.data.inviteCode;
    if (!code) {
      return;
    }
    wx.setClipboardData({
      data: code,
      success: function () {
        wx.showToast({ title: '邀请码已复制', icon: 'none' });
      }
    });
  }
});
