var request = require('../../utils/request.js');

Page({
  data: {
    deadlines: [],
    loading: true,
    subscribe_tmpl_id: '',
    subscribed: false
  },
  onShow: function () {
    this.loadDeadlines();
  },
  loadDeadlines: function () {
    var that = this;
    this.setData({ loading: true });
    request.request('/api/calendar/deadlines', 'GET')
      .then(function (res) {
        var list = (res && res.deadlines) || [];
        list.forEach(function (it) {
          var d = it.days_left;
          it.level = d <= 3 ? 'urgent' : (d <= 7 ? 'near' : 'normal');
          it.days_text = d === 0 ? '今天截止' : ('剩 ' + d + ' 天');
        });
        that.setData({ deadlines: list, loading: false, subscribe_tmpl_id: (res && res.subscribe_tmpl_id) || '' });
      })
      .catch(function () {
        that.setData({ loading: false });
      });
  },
  onSubscribe: function () {
    var that = this;
    var tmpl = this.data.subscribe_tmpl_id;
    if (!tmpl) {
      wx.showToast({ title: '订阅模板暂未配置', icon: 'none' });
      return;
    }
    wx.requestSubscribeMessage({
      tmplIds: [tmpl],
      success: function () {
        request.request('/api/user/subscribe', 'POST', {
          openid: getApp().globalData.openid,
          tmpl_id: tmpl
        }).then(function (r) {
          if (r && r.ok) {
            that.setData({ subscribed: true });
            wx.showToast({ title: '已开启，征期前自动提醒', icon: 'success' });
          } else {
            wx.showToast({ title: (r && r.msg) || '记录失败', icon: 'none' });
          }
        }).catch(function () {
          wx.showToast({ title: '记录失败', icon: 'none' });
        });
      },
      fail: function () {
        wx.showToast({ title: '未授权，将无法收到提醒', icon: 'none' });
      }
    });
  },
  onShare: function () {
    wx.showToast({ title: '征期提醒可分享给同事', icon: 'none' });
  }
});
