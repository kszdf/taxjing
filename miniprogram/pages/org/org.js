var store = require('../../utils/store.js');
var request = require('../../utils/request.js');

var TABS = [
  { key: 'me', name: '概览' },
  { key: 'clients', name: '客户档案' },
  { key: 'answers', name: '机构口径库' },
  { key: 'ask', name: '团队问答' },
  { key: 'tasks', name: '作业任务' },
  { key: 'members', name: '席位成员' },
  { key: 'risk', name: '风险扫描' }
];

Page({
  data: {
    token: '',
    orgInfo: {},
    role: '',
    tab: 'me',
    tabs: TABS,
    // 登录表单（默认填演示机构，便于体验）
    lgCode: 'ORGDEMO', lgLogin: 'owner', lgPwd: 'demo1234',
    // 概览
    me: {},
    // 客户档案
    clients: [], cName: '', cCredit: '', cIndustry: '', cRegion: '', cType: '',
    // 机构口径库
    answers: [], aCat: '', aPattern: '', aMd: '', pPost: '', pReply: '',
    // 团队问答
    askQ: '', askR: '',
    // 作业任务
    tasks: [], selMembers: [], selClients: '',
    tTitle: '', tAssignee: '', tClient: '', tDue: '', tType: '申报',
    // 席位成员
    members: [], mLogin: '', mName: '', mRole: 'member',
    // 风险扫描
    riskRules: [], riskResult: ''
  },

  onShow: function () {
    var token = store.getOrgToken();
    if (!token) { this.setData({ token: '' }); return; }
    var info = store.getOrgInfo();
    this.setData({ token: token, orgInfo: info, role: info.role || '' });
    this.loadMe();
    this.switchTab(this.data.tab);
  },

  // —— 机构登录（绑定到当前微信 openid，打通统一账号）——
  onLgInput: function (e) {
    this.setData({ ['lg' + e.currentTarget.dataset.f]: e.detail.value });
  },
  doLogin: function () {
    var that = this;
    var openid = store.getOpenid();
    if (!openid) {
      wx.showToast({ title: '请先在小程序授权登录', icon: 'none' });
      return;
    }
    request.orgRequest('/api/org/login', 'POST', {
      org_code: this.data.lgCode, login: this.data.lgLogin,
      password: this.data.lgPwd, openid: openid
    }).then(function (res) {
      store.setOrgToken(res.token);
      store.setOrgInfo({ org_id: res.org.id, org_name: res.org.name, org_code: res.org.org_code, role: res.member.role });
      that.setData({ token: res.token, orgInfo: store.getOrgInfo(), role: res.member.role });
      that.loadMe();
      that.switchTab('me');
      wx.showToast({ title: '已登录机构端', icon: 'success' });
    });
  },
  doLogout: function () {
    store.clearOrg();
    this.setData({ token: '', orgInfo: {}, role: '', me: {} });
  },

  switchTab: function (t) {
    this.setData({ tab: t });
    if (t === 'clients') this.loadClients();
    else if (t === 'answers') this.loadAnswers();
    else if (t === 'tasks') this.loadTasks();
    else if (t === 'members') this.loadMembers();
    else if (t === 'risk') this.loadRiskRules();
  },
  onTabTap: function (e) { this.switchTab(e.currentTarget.dataset.key); },

  loadMe: function () {
    var that = this;
    request.orgRequest('/api/org/me', 'GET', {}, this.data.token).then(function (res) {
      that.setData({ me: res });
    });
  },

  // —— 客户档案 ——
  loadClients: function () {
    var that = this;
    request.orgRequest('/api/org/clients', 'GET', {}, this.data.token).then(function (res) {
      that.setData({ clients: res.clients || [] });
    });
  },
  onCInput: function (e) { this.setData({ [e.currentTarget.dataset.f]: e.detail.value }); },
  saveClient: function () {
    var that = this;
    if (!this.data.cName) { wx.showToast({ title: '客户名称必填', icon: 'none' }); return; }
    request.orgRequest('/api/org/client/save', 'POST', {
      name: this.data.cName, credit_no: this.data.cCredit, industry: this.data.cIndustry,
      region: this.data.cRegion, taxpayer_type: this.data.cType
    }, this.data.token).then(function () {
      that.setData({ cName: '', cCredit: '', cIndustry: '', cRegion: '', cType: '' });
      that.loadClients();
      wx.showToast({ title: '已保存', icon: 'success' });
    });
  },

  // —— 机构口径库 ——
  loadAnswers: function () {
    var that = this;
    request.orgRequest('/api/org/answers', 'GET', {}, this.data.token).then(function (res) {
      that.setData({ answers: res.answers || [] });
    });
  },
  onAInput: function (e) { this.setData({ [e.currentTarget.dataset.f]: e.detail.value }); },
  saveAnswer: function () {
    var that = this;
    if (!this.data.aPattern || !this.data.aMd) { wx.showToast({ title: '命中关键词与标准答案必填', icon: 'none' }); return; }
    request.orgRequest('/api/org/answer/save', 'POST', {
      category: this.data.aCat, question_pattern: this.data.aPattern,
      answer_md: this.data.aMd, status: 'draft'
    }, this.data.token).then(function () {
      that.setData({ aCat: '', aPattern: '', aMd: '' });
      that.loadAnswers();
      wx.showToast({ title: '已存为草稿，待审定', icon: 'success' });
    });
  },
  promoteReply: function () {
    var that = this;
    if (!this.data.pPost || !this.data.pReply) { wx.showToast({ title: '需填 post_id 与 reply_id', icon: 'none' }); return; }
    request.orgRequest('/api/org/promote-reply', 'POST', {
      post_id: parseInt(this.data.pPost, 10), reply_id: parseInt(this.data.pReply, 10)
    }, this.data.token).then(function (res) {
      that.setData({ pPost: '', pReply: '' });
      that.loadAnswers();
      wx.showToast({ title: res.msg || '已沉淀', icon: 'none' });
    });
  },

  // —— 团队问答 ——
  onAskInput: function (e) { this.setData({ askQ: e.detail.value }); },
  askTeam: function () {
    var that = this;
    if (!this.data.askQ) { wx.showToast({ title: '请输入问题', icon: 'none' }); return; }
    request.orgRequest('/api/org/ask', 'POST', { question: this.data.askQ }, this.data.token).then(function (res) {
      that.setData({ askR: res.answer || (res.msg || '已提交，待专家确认'), askQ: '' });
    });
  },

  // —— 作业任务 ——
  loadTasks: function () {
    var that = this;
    request.orgRequest('/api/org/tasks?tab=all', 'GET', {}, this.data.token).then(function (res) {
      that.setData({ tasks: res.tasks || [] });
    });
    request.orgRequest('/api/org/members', 'GET', {}, this.data.token).then(function (res) {
      that.setData({ selMembers: res.members || [] });
    });
    request.orgRequest('/api/org/clients', 'GET', {}, this.data.token).then(function (res) {
      that.setData({ selClients: res.clients || [] });
    });
  },
  onTInput: function (e) { this.setData({ [e.currentTarget.dataset.f]: e.detail.value }); },
  onTSelect: function (e) { this.setData({ [e.currentTarget.dataset.f]: e.detail.value }); },
  createTask: function () {
    var that = this;
    if (!this.data.tTitle) { wx.showToast({ title: '任务标题必填', icon: 'none' }); return; }
    var assignee = this.data.selMembers[this.data.tAssignee] || {};
    var client = this.data.selClients[this.data.tClient] || {};
    request.orgRequest('/api/org/task/create', 'POST', {
      title: this.data.tTitle, assignee_member_id: assignee.id || 0,
      client_id: client.id || 0, due: this.data.tDue, task_type: this.data.tType
    }, this.data.token).then(function () {
      that.setData({ tTitle: '', tAssignee: '', tClient: '', tDue: '', tType: '申报' });
      that.loadTasks();
      wx.showToast({ title: '已下派', icon: 'success' });
    });
  },

  // —— 席位成员 ——
  loadMembers: function () {
    var that = this;
    request.orgRequest('/api/org/members', 'GET', {}, this.data.token).then(function (res) {
      that.setData({ members: res.members || [] });
    });
  },
  onMInput: function (e) { this.setData({ [e.currentTarget.dataset.f]: e.detail.value }); },
  inviteMember: function () {
    var that = this;
    if (!this.data.mLogin) { wx.showToast({ title: '登录名必填', icon: 'none' }); return; }
    request.orgRequest('/api/org/member/invite', 'POST', {
      login: this.data.mLogin, name: this.data.mName, role: this.data.mRole
    }, this.data.token).then(function () {
      that.setData({ mLogin: '', mName: '', mRole: 'member' });
      that.loadMembers();
      wx.showToast({ title: '已邀请', icon: 'success' });
    });
  },

  // —— 风险扫描 ——
  loadRiskRules: function () {
    var that = this;
    request.orgRequest('/api/org/risk/rules', 'GET', {}, this.data.token).then(function (res) {
      that.setData({ riskRules: res.rules || [] });
    });
  },
  batchRisk: function () {
    var that = this;
    request.orgRequest('/api/org/risk/batch', 'POST', {}, this.data.token).then(function (res) {
      that.setData({ riskResult: JSON.stringify(res) });
      wx.showToast({ title: '扫描完成', icon: 'success' });
    });
  },

  goBack: function () { wx.navigateBack(); }
});
