var store = require('./utils/store.js');
var request = require('./utils/request.js');

App({
  globalData: {
    openid: ''
  },
  onLaunch: function () {
    var openid = store.getOpenid();
    if (!openid) {
      var that = this;
      // 优先用 wx.login 换真实 openid（稳定身份）；失败再降级为普通注册
      wx.login({
        success: function (loginRes) {
          var body = loginRes.code ? { code: loginRes.code } : {};
          request.request('/api/register', 'POST', body)
            .then(function (res) {
              store.setOpenid(res.openid);
              that.globalData.openid = res.openid;
            })
            .catch(function () {});
        },
        fail: function () {
          request.request('/api/register', 'POST', {})
            .then(function (res) {
              store.setOpenid(res.openid);
              that.globalData.openid = res.openid;
            })
            .catch(function () {});
        }
      });
    } else {
      this.globalData.openid = openid;
    }
  }
});
