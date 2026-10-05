var request = require('../../utils/request.js');

Page({
  data: {
    corpid: '',
    qrcodeUrl: '',
    tip: '',
    loading: true
  },
  onShow: function () {
    var that = this;
    request.request('/api/kefu/info', 'GET')
      .then(function (res) {
        that.setData({
          corpid: res.corpid || '',
          qrcodeUrl: res.qrcode_url || '',
          tip: res.tip || '',
          loading: false
        });
      })
      .catch(function () {
        that.setData({ loading: false });
      });
  },
  onCopyWechat: function () {
    var corpid = this.data.corpid;
    if (!corpid) {
      wx.showToast({ title: '客服微信暂未配置', icon: 'none' });
      return;
    }
    wx.setClipboardData({
      data: corpid,
      success: function () {
        wx.showToast({ title: '客服微信已复制', icon: 'none' });
      }
    });
  },
  onOpenSession: function () {
    wx.showToast({ title: '请通过复制的客服微信联系我们', icon: 'none' });
  },
  onRecharge: function () {
    wx.showModal({
      title: '法币充值说明',
      content: '积分充值需通过客服微信完成：\n1. 复制客服微信并添加好友；\n2. 说明充值金额；\n3. 按客服指引完成付款后，积分将自动到账。',
      showCancel: false,
      confirmText: '知道了'
    });
  }
});
