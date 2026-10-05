var request = require('../../utils/request.js');

// 统一给条目加上中文状态与样式类（列表与搜索结果共用）
function decorate(p) {
  var s = p.status || '';
  p.status_label = p.status_label || (s === 'active' ? '生效中'
    : (s === 'partially_invalid' ? '部分失效'
      : (s === 'indexed' ? '仅目录' : (s === 'pending_review' ? '待复核' : '已失效'))));
  p.cls = s === 'active' ? 'tag-green'
    : (s === 'partially_invalid' ? 'tag-yellow'
      : (s === 'indexed' ? 'tag-blue' : 'tag-red'));
  return p;
}

Page({
  data: {
    policies: [],
    loading: true,
    keyword: '',
    mode: 'list',
    hint: ''
  },
  onShow: function () {
    if (this.data.mode === 'search' && this.data.keyword) {
      this.doSearch();
    } else {
      this.loadList();
    }
  },
  loadList: function () {
    var that = this;
    this.setData({ loading: true, mode: 'list' });
    request.request('/api/policy/list', 'GET')
      .then(function (res) {
        var list = (res.policies || []).map(decorate);
        that.setData({
          policies: list,
          loading: false,
          hint: '已收录并生效的政策 ' + list.length + ' 份（可搜全量目录）'
        });
      })
      .catch(function () { that.setData({ loading: false }); });
  },
  onInput: function (e) {
    this.setData({ keyword: e.detail.value });
  },
  onSearch: function () {
    var kw = (this.data.keyword || '').trim();
    if (!kw) { this.loadList(); return; }
    this.doSearch();
  },
  doSearch: function () {
    var that = this, kw = (this.data.keyword || '').trim();
    if (!kw) { this.loadList(); return; }
    this.setData({ loading: true, mode: 'search' });
    request.request('/api/policy/search?q=' + encodeURIComponent(kw) + '&limit=50', 'GET')
      .then(function (res) {
        var list = (res.items || []).map(decorate);
        that.setData({
          policies: list,
          loading: false,
          hint: '“' + kw + '” 命中 ' + list.length + ' 份（含仅收录目录、尚未核对正文的文件）'
        });
      })
      .catch(function () { that.setData({ loading: false }); });
  },
  onClear: function () {
    this.setData({ keyword: '', mode: 'list' });
    this.loadList();
  },
  onCopySource: function (e) {
    var url = e.currentTarget.dataset.url;
    if (!url) { return; }
    wx.setClipboardData({
      data: url,
      success: function () {
        wx.showToast({ title: '官方原文链接已复制，可在浏览器打开', icon: 'none' });
      }
    });
  },
  onItemTap: function (e) {
    var ds = e.currentTarget.dataset;
    if (ds.status === 'indexed' || ds.status === 'pending_review') {
      wx.showToast({ title: '该文件仅收录目录，尚未核对正文', icon: 'none' });
      return;
    }
    wx.navigateTo({ url: '/pages/policyDetail/policyDetail?doc=' + encodeURIComponent(ds.doc) });
  }
});
