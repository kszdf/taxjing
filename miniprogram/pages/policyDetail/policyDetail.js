var request = require('../../utils/request.js');

Page({
  data: {
    doc: '',
    title: '',
    docNumber: '',
    status: '',
    clauses: [],
    loading: true
  },
  onLoad: function (options) {
    var doc = options.doc || '';
    this.setData({ doc: doc });
    this.loadDetail(doc);
  },
  loadDetail: function (doc) {
    var that = this;
    this.setData({ loading: true });
    request.request('/api/policy/detail?doc=' + encodeURIComponent(doc), 'GET')
      .then(function (res) {
        that.setData({
          title: res.title || '',
          docNumber: res.doc_number || '',
          status: res.status || '',
          clauses: res.clauses || [],
          loading: false
        });
      })
      .catch(function () {
        that.setData({ loading: false });
      });
  }
});
