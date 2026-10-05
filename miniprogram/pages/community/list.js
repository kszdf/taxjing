var store = require('../../utils/store.js');
var request = require('../../utils/request.js');

Page({
  data: {
    tab: 'latest',
    scene: '',
    sceneTags: [],
    posts: [],
    loading: true,
    page: 1,
    // 我的社区资产（M2）
    myRep: 0,
    myPoints: 0,
    myAccepted: 0,
    // 发帖表单
    showForm: false,
    formTitle: '',
    formContent: '',
    formScene: '其他',
    formAnonymous: false,
    formBounty: '',
    submitting: false
  },

  onShow: function () {
    this.loadProfile();
    this.loadList();
  },

  loadProfile: function () {
    var openid = store.getOpenid() || '';
    if (!openid) { return; }
    var that = this;
    request.request('/api/community/profile?openid=' + openid, 'GET')
      .then(function (res) {
        if (res && res.ok && res.profile) {
          that.setData({
            myRep: res.profile.reputation,
            myPoints: res.profile.points_balance,
            myAccepted: res.profile.accepted_count
          });
        }
      }).catch(function () {});
  },

  loadList: function () {
    var that = this;
    var qs = '/api/community/list?tab=' + this.data.tab + '&page=' + this.data.page;
    if (this.data.scene) {
      qs += '&scene=' + encodeURIComponent(this.data.scene);
    }
    this.setData({ loading: true });
    request.request(qs, 'GET')
      .then(function (res) {
        that.setData({
          posts: res.posts || [],
          sceneTags: res.scene_tags || [],
          loading: false
        });
      })
      .catch(function () {
        that.setData({ loading: false });
      });
  },

  onTabTap: function (e) {
    var t = e.currentTarget.dataset.tab;
    if (t === this.data.tab && !this.data.scene) { return; }
    this.setData({ tab: t, scene: '', page: 1 });
    this.loadList();
  },

  onSceneTap: function (e) {
    var s = e.currentTarget.dataset.scene;
    this.setData({ scene: this.data.scene === s ? '' : s, page: 1 });
    this.loadList();
  },

  onPostTap: function (e) {
    var id = e.currentTarget.dataset.id;
    wx.navigateTo({ url: '/pages/community/detail?id=' + id });
  },

  // ---- 发帖 ----
  onToggleForm: function () {
    this.setData({ showForm: !this.data.showForm });
  },

  onTitleInput: function (e) { this.setData({ formTitle: e.detail.value }); },
  onContentInput: function (e) { this.setData({ formContent: e.detail.value }); },

  onSceneChange: function (e) {
    this.setData({ formScene: this.data.sceneTags[e.detail.value] || '其他' });
  },

  onAnonymousChange: function (e) {
    this.setData({ formAnonymous: e.detail.value });
  },

  onBountyInput: function (e) {
    this.setData({ formBounty: e.detail.value });
  },

  onSubmitPost: function () {
    var title = (this.data.formTitle || '').trim();
    var content = (this.data.formContent || '').trim();
    if (!title) { wx.showToast({ title: '请输入标题', icon: 'none' }); return; }
    if (!content) { wx.showToast({ title: '请输入问题描述', icon: 'none' }); return; }
    var bounty = parseInt(this.data.formBounty || '0', 10) || 0;
    if (bounty < 0 || bounty > 500) {
      wx.showToast({ title: '悬赏须在 0~500 之间', icon: 'none' }); return;
    }
    var openid = store.getOpenid();
    if (!openid) { wx.showToast({ title: '系统初始化中，请稍后再试', icon: 'none' }); return; }
    var that = this;
    this.setData({ submitting: true });
    request.request('/api/community/post', 'POST', {
      openid: openid,
      title: title,
      content: content,
      scene_tag: this.data.formScene,
      is_anonymous: this.data.formAnonymous ? 1 : 0,
      bounty_points: bounty
    }).then(function (res) {
      that.setData({
        submitting: false, showForm: false,
        formTitle: '', formContent: '', formAnonymous: false, formBounty: ''
      });
      if (res && res.ok) {
        wx.showToast({ title: res.bounty ? ('已发布，冻结 ' + res.bounty + ' 积分悬赏') : '已发布，AI参考生成中', icon: 'none' });
      } else {
        wx.showToast({ title: (res && res.msg) || '发布失败', icon: 'none' });
      }
      that.setData({ page: 1, tab: 'latest', scene: '' });
      that.loadProfile();
      that.loadList();
    }).catch(function () {
      that.setData({ submitting: false });
    });
  }
});
