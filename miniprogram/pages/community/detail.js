var store = require('../../utils/store.js');
var request = require('../../utils/request.js');

Page({
  data: {
    id: 0,
    post: null,
    aiAnswer: '',
    aiRefs: [],
    aiLoading: false,
    replies: [],
    disclaimer: '',
    isOwner: false,
    loading: true,
    // 回复表单
    replyContent: '',
    showPicker: false,
    searchQ: '',
    searchResults: [],
    selRefs: [],
    submitting: false
  },

  onLoad: function (options) {
    this.setData({ id: options.id || 0 });
  },

  onShow: function () {
    this.loadDetail();
  },

  onPullDownRefresh: function () {
    this.loadDetail();
  },

  loadDetail: function () {
    var that = this;
    var openid = store.getOpenid() || '';
    this.setData({ loading: true });
    request.request('/api/community/detail?id=' + this.data.id + '&openid=' + openid, 'GET')
      .then(function (res) {
        that.setData({
          post: res.post,
          aiAnswer: res.ai_answer || '',
          aiRefs: res.ai_policy_refs || [],
          aiLoading: !res.ai_answer,
          replies: res.replies || [],
          disclaimer: res.disclaimer || '',
          isOwner: !!res.is_owner,
          loading: false
        });
        wx.stopPullDownRefresh();
      })
      .catch(function () {
        that.setData({ loading: false });
        wx.stopPullDownRefresh();
      });
  },

  onRefreshAi: function () {
    this.loadDetail();
  },

  onRefTap: function (e) {
    var doc = e.currentTarget.dataset.doc;
    wx.navigateTo({ url: '/pages/policyDetail/policyDetail?doc=' + encodeURIComponent(doc) });
  },

  // ---- 点赞 ----
  onLikeTap: function (e) {
    var rid = e.currentTarget.dataset.id;
    var openid = store.getOpenid();
    if (!openid) { wx.showToast({ title: '系统初始化中', icon: 'none' }); return; }
    var that = this;
    request.request('/api/community/like', 'POST', { openid: openid, reply_id: rid })
      .then(function () {
        that.loadDetail();
      }).catch(function () {});
  },

  // ---- 采纳 ----
  onAcceptTap: function (e) {
    var rid = e.currentTarget.dataset.id;
    var that = this;
    var bounty = this.data.post.bounty_points || 0;
    var content = bounty > 0
      ? ('采纳后答主获得 20 积分 + ' + bounty + ' 悬赏 + 10 声望，本帖标记为已解决。')
      : '采纳后答主获得 20 积分 + 10 声望，本帖标记为已解决。';
    wx.showModal({
      title: '采纳该回答',
      content: content,
      success: function (r) {
        if (!r.confirm) { return; }
        request.request('/api/community/accept', 'POST', {
          openid: store.getOpenid(),
          post_id: that.data.id,
          reply_id: rid
        }).then(function () {
          wx.showToast({ title: '已采纳', icon: 'success' });
          that.loadDetail();
        }).catch(function () {});
      }
    });
  },

  // ---- 回复 ----
  onReplyInput: function (e) { this.setData({ replyContent: e.detail.value }); },

  onTogglePicker: function () {
    this.setData({ showPicker: !this.data.showPicker });
  },

  onSearchInput: function (e) { this.setData({ searchQ: e.detail.value }); },

  onDoSearch: function () {
    var q = (this.data.searchQ || '').trim();
    if (!q) { return; }
    var that = this;
    request.request('/api/policy/search?q=' + encodeURIComponent(q) + '&limit=10', 'GET')
      .then(function (res) {
        that.setData({ searchResults: res.items || [] });
      }).catch(function () {});
  },

  onPickRef: function (e) {
    var doc = e.currentTarget.dataset.doc;
    var refs = this.data.selRefs.slice();
    var i = refs.indexOf(doc);
    if (i >= 0) { refs.splice(i, 1); } else { refs.push(doc); }
    this.setData({ selRefs: refs });
  },

  onRemoveRef: function (e) {
    var doc = e.currentTarget.dataset.doc;
    var refs = this.data.selRefs.slice();
    refs.splice(refs.indexOf(doc), 1);
    this.setData({ selRefs: refs });
  },

  onSubmitReply: function () {
    var c = (this.data.replyContent || '').trim();
    if (!c) { wx.showToast({ title: '请输入回答内容', icon: 'none' }); return; }
    var openid = store.getOpenid();
    if (!openid) { wx.showToast({ title: '系统初始化中', icon: 'none' }); return; }
    var that = this;
    this.setData({ submitting: true });
    request.request('/api/community/reply', 'POST', {
      openid: openid,
      post_id: this.data.id,
      content: c,
      policy_refs: this.data.selRefs
    }).then(function () {
      that.setData({ submitting: false, replyContent: '', selRefs: [], showPicker: false, searchResults: [] });
      wx.showToast({ title: '回答已提交', icon: 'success' });
      that.loadDetail();
    }).catch(function () {
      that.setData({ submitting: false });
    });
  }
});
