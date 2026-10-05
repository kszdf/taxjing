function getOpenid() {
  return wx.getStorageSync('openid') || '';
}

function setOpenid(openid) {
  wx.setStorageSync('openid', openid);
}

function getOrgToken() {
  return wx.getStorageSync('org_token') || '';
}

function setOrgToken(t) {
  wx.setStorageSync('org_token', t || '');
}

function getOrgInfo() {
  try { return JSON.parse(wx.getStorageSync('org_info') || '{}'); } catch (e) { return {}; }
}

function setOrgInfo(o) {
  wx.setStorageSync('org_info', JSON.stringify(o || {}));
}

function clearOrg() {
  wx.removeStorageSync('org_token');
  wx.removeStorageSync('org_info');
}

module.exports = {
  getOpenid: getOpenid,
  setOpenid: setOpenid,
  getOrgToken: getOrgToken,
  setOrgToken: setOrgToken,
  getOrgInfo: getOrgInfo,
  setOrgInfo: setOrgInfo,
  clearOrg: clearOrg
};
