var config = require('../config.js');

/**
 * 统一请求封装：自动拼接 BASE 地址、发送 JSON、处理失败。
 * @param {string} path   接口路径，如 '/api/state?openid=xxx'
 * @param {string} method GET / POST
 * @param {object} data   请求体（POST 时）
 * @returns {Promise}     成功 resolve(body)，失败 reject 并 toast 提示
 */
function request(path, method, data) {
  method = (method || 'GET').toUpperCase();
  return new Promise(function (resolve, reject) {
    wx.request({
      url: config.API_BASE + path,
      method: method,
      data: data || {},
      header: { 'content-type': 'application/json' },
      success: function (res) {
        var body = res.data;
        if (res.statusCode >= 200 && res.statusCode < 300 && body && body.ok === true) {
          resolve(body);
        } else {
          var msg = (body && body.msg) || ('请求失败(' + res.statusCode + ')');
          wx.showToast({ title: msg, icon: 'none' });
          reject(body);
        }
      },
      fail: function (err) {
        wx.showToast({ title: '网络请求失败，请检查网络', icon: 'none' });
        reject(err);
      }
    });
  });
}

function orgRequest(path, method, data, token) {
  method = (method || 'GET').toUpperCase();
  return new Promise(function (resolve, reject) {
    wx.request({
      url: config.API_BASE + path,
      method: method,
      data: data || {},
      header: { 'content-type': 'application/json', 'X-Org-Token': token || '' },
      success: function (res) {
        var body = res.data;
        if (res.statusCode >= 200 && res.statusCode < 300 && body && body.ok === true) {
          resolve(body);
        } else {
          var msg = (body && body.msg) || ('请求失败(' + res.statusCode + ')');
          wx.showToast({ title: msg, icon: 'none' });
          reject(body);
        }
      },
      fail: function (err) {
        wx.showToast({ title: '网络请求失败，请检查网络', icon: 'none' });
        reject(err);
      }
    });
  });
}

function converse(path, method, data, orgToken) {
  method = (method || 'GET').toUpperCase();
  var header = { 'content-type': 'application/json' };
  if (orgToken) header['X-Org-Token'] = orgToken;
  return new Promise(function (resolve, reject) {
    wx.request({
      url: config.API_BASE + path,
      method: method,
      data: data || {},
      header: header,
      success: function (res) {
        var body = res.data;
        if (res.statusCode >= 200 && res.statusCode < 300 && body && body.ok === true) {
          resolve(body);
        } else {
          var msg = (body && body.msg) || ('请求失败(' + res.statusCode + ')');
          wx.showToast({ title: msg, icon: 'none' });
          reject(body);
        }
      },
      fail: function (err) {
        wx.showToast({ title: '网络请求失败，请检查网络', icon: 'none' });
        reject(err);
      }
    });
  });
}

module.exports = {
  request: request,
  orgRequest: orgRequest,
  converse: converse
};
